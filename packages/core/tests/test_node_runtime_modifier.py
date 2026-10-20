"""Modified: cover exact class maps, scoped settings, real repair nodes and concurrency."""
# Modified 2026-10-14: request-only repair API, standalone test runners and terminal close.
# Modified 2026-10-14: plain execution boundaries and zero request-time native allocations.
# Modified 2026-10-15: merged repair request model and same-input per-run isolation.
# Modified 2026-10-15: verify repair outputs and native execution without stage bookkeeping.
# Modified 2026-10-20: cover the interactive amendment gate, guidance and skip.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
import inspect
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import pytest
from ag_ui_workflow import RunResult, StepRunOutput, WorkflowEngine, WorkflowParam, WorkflowStepNode

from flowx_core.runtime import RunNodeTest
from flowx_core.runtime.run_node_test import _execute_node_test
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    ExecuteNodeTest,
    GenerateNodeTestFile,
    NodeRuntimeModifierPipeline,
    NodeRuntimeModifierRequest,
)
import flowx_core.workflows.node_runtime_modifier as modifier_module


@dataclass
class _TestWriter(GenerateNodeTestFile):
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def write_test_from_node_file(
        self,
        node_file_path: str,
        output_path: str,
        **_kwargs: object,
    ) -> Path:
        self.calls.append((node_file_path, output_path))
        target = Path(output_path)
        target.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
        return target


@dataclass
class _NodeWriter(AmendNodeFromTestLog):
    calls: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def amend_code_with_feedback(
        self,
        code_path: str,
        amendment: str,
        **kwargs: object,
    ) -> Path:
        self.calls.append(
            {
                "code_path": code_path,
                "amendment": amendment,
                **kwargs,
            }
        )
        target = Path(code_path)
        target.write_text("# amended node\n", encoding="utf-8")
        return target


@dataclass
class _GatedNodeWriter(_NodeWriter):
    """Record amendments through the production INPUT_REQUIRED wait callback."""

    def __post_init__(self) -> None:
        AmendNodeFromTestLog.__post_init__(self)


def _assert_executor_pristine(node, run_subprocess, timeout_expired):
    assert type(node) is ExecuteNodeTest
    assert node.node_name == node.cwd == ""
    assert node.test_path == node.log_path == Path()
    assert node.timeout == 60
    assert node.command == []
    assert node.result is None
    assert node.run_subprocess is run_subprocess
    assert node.timeout_expired is timeout_expired
    assert "process_input" not in node.__dict__


def test_repair_request_is_the_only_model_with_required_keyword_inputs_and_fresh_outputs() -> None:
    required = {"node_file_name", "workflow_graph", "working_directory"}
    output_defaults = {
        "test_file_path": "", "log_file_path": "", "test_log": "", "test_result": None,
        "amended_node_file_path": "",
    }
    removed_fields = {"workflow_name", "skipped_stages", "completed_stages"}
    assert is_dataclass(NodeRuntimeModifierRequest)
    assert not NodeRuntimeModifierRequest.__dataclass_params__.frozen
    assert not hasattr(modifier_module, "NodeRuntimeModifierContext")
    assert "NodeRuntimeModifierContext" not in modifier_module.__all__
    assert not hasattr(modifier_module, "_context_from_state")
    assert modifier_module.__all__.count("NodeRuntimeModifierRequest") == 1
    model_fields = {item.name: item for item in fields(NodeRuntimeModifierRequest)}
    assert removed_fields.isdisjoint(model_fields)
    assert {
        name for name, item in model_fields.items()
        if item.init and item.default is MISSING and item.default_factory is MISSING
    } == required
    assert {name for name, item in model_fields.items() if not item.init} == set(output_defaults)
    signature = inspect.signature(NodeRuntimeModifierRequest)
    assert removed_fields.isdisjoint(signature.parameters)
    assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in signature.parameters.values())
    inputs = {"node_file_name": "ExampleNode.py", "workflow_graph": {}, "working_directory": "."}
    for name in required:
        with pytest.raises(TypeError, match=name):
            NodeRuntimeModifierRequest(**{key: value for key, value in inputs.items() if key != name})
    for name, value in output_defaults.items():
        with pytest.raises(TypeError, match=name):
            NodeRuntimeModifierRequest(**inputs, **{name: value})
    for name in removed_fields:
        with pytest.raises(TypeError, match=name):
            NodeRuntimeModifierRequest(**inputs, **{name: None})
    first, second = NodeRuntimeModifierRequest(**inputs), NodeRuntimeModifierRequest(**inputs)
    for name, value in output_defaults.items():
        assert getattr(first, name) == getattr(second, name) == value
    assert all(not hasattr(request, name) for request in (first, second) for name in removed_fields)
    assert first.state is not second.state
    first.test_result = {"ok": False}
    assert second.test_result is None
    second.test_result = {"ok": True}
    assert first.test_result is not second.test_result
    first.test_result["only_first"] = True
    assert second.test_result == {"ok": True}
    assert modifier_module._request_from_state({"node_runtime_modifier": first}) is first


@pytest.mark.parametrize("state", [{}, {"node_runtime_modifier": None}, {"node_runtime_modifier": object()}])
def test_repair_state_requires_the_merged_request(state) -> None:
    with pytest.raises(TypeError, match="node runtime modifier request is missing or has an unexpected type"):
        modifier_module._request_from_state(state)


@pytest.mark.parametrize("concurrent,pool_size", [(False, 1), (True, 1), (True, 2)])
@pytest.mark.parametrize("stale_outputs", [False, True])
def test_repair_repeated_or_concurrent_same_input_uses_fresh_native_requests(
    monkeypatch, tmp_path: Path, concurrent: bool, pool_size: int, stale_outputs: bool,
) -> None:
    injected = object()
    graph = {"nodes": [{"name": "ExampleNode", "depends": []}]}
    state = {"injected": injected, "node_runtime_modifier": object()}
    workdir_input = str(tmp_path / ".." / tmp_path.name)
    caller = NodeRuntimeModifierRequest(
        node_file_name="ExampleNode.py", workflow_graph=graph, working_directory=workdir_input,
        skip_generate_node_test=True, skip_run_node_test=True, skip_amend_node=True,
        node_name="LogicalNode", state=state,
    )
    if stale_outputs:
        caller.test_file_path = "stale_test.py"
        caller.log_file_path = "stale.log"
        caller.test_log = "stale failure"
        caller.test_result = {"ok": False, "command": ["stale"]}
        caller.amended_node_file_path = "stale_node.py"
    caller_snapshot = {item.name: getattr(caller, item.name) for item in fields(caller)}
    state_snapshot = dict(state)
    graph_snapshot = json.dumps(graph)
    entered_requests, native_states, run_results = [], [], []
    barrier = Barrier(2) if concurrent else None

    @dataclass
    class StateWriter(_TestWriter):
        def process_input(self, user_input, dependency_results, session_state):
            request = modifier_module._request_from_state(session_state)
            assert type(request) is NodeRuntimeModifierRequest
            assert self.get_workflow_param().state is session_state
            assert request.state is state
            assert session_state["injected"] is injected
            assert "native_visit" not in session_state
            session_state["native_visit"] = ["generate_node_test"]
            native_states.append(session_state)
            return super().process_input(user_input, dependency_results, session_state)

    @dataclass
    class StateAmender(_NodeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            request = modifier_module._request_from_state(session_state)
            assert self.get_workflow_param().state is session_state
            assert any(observed is session_state for observed in native_states)
            assert set(dependency_results) == {"run_node_test"}
            assert dependency_results["run_node_test"].derived == {"log_file_path": ""}
            assert request.test_result is None
            assert request.test_log == request.log_file_path == ""
            assert session_state["native_visit"] == ["generate_node_test"]
            session_state["native_visit"].append("amend_node")
            return super().process_input(user_input, dependency_results, session_state)

    real_run = modifier_module.run_workflow

    def capture_fresh_request(engine, **kwargs):
        request = kwargs["state"]["node_runtime_modifier"]
        assert request is not caller
        assert request.state is state
        assert kwargs["state"]["injected"] is injected
        assert request.working_directory == str(tmp_path.resolve())
        assert request.test_file_path == request.log_file_path == request.test_log == ""
        assert request.amended_node_file_path == ""
        assert request.test_result is None
        entered_requests.append(request)
        if barrier is not None:
            barrier.wait(timeout=10)
        result = real_run(engine, **kwargs)
        run_results.append(result)
        return result

    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    monkeypatch.setattr(modifier_module, "run_workflow", capture_fresh_request)

    def unexpected_run(*args, **kwargs):
        del args, kwargs
        pytest.fail("skipped subprocess")

    modifier = NodeRuntimeModifierPipeline(
        StateWriter(), StateAmender(), run_subprocess=unexpected_run,
    )
    try:
        if concurrent:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(modifier.run, caller) for _ in range(2)]
                requests = [future.result(timeout=15) for future in futures]
        else:
            requests = [modifier.run(caller), modifier.run(caller)]
        assert len(native_states) == len(entered_requests) == len(run_results) == 2
        assert requests[0] is not requests[1]
        assert native_states[0] is not native_states[1]
        assert run_results[0].request_id != run_results[1].request_id
        for result in run_results:
            assert result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
            assert set(result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
            assert result.outputs["generate_node_test"].derived == {"test_file_path": ""}
            assert result.outputs["run_node_test"].derived == {"log_file_path": ""}
            assert result.outputs["amend_node"].derived == {
                "amended_node_file_path": str(tmp_path / "ExampleNode.py"),
            }
        assert len({request.workflow_graph for request in requests}) == 2
        for request in requests:
            assert type(request) is NodeRuntimeModifierRequest
            assert any(observed is request for observed in entered_requests)
            assert any(native["node_runtime_modifier"] is request for native in native_states)
            assert request.state is state
            assert request.working_directory == str(tmp_path.resolve())
            assert request.node_file_name == caller.node_file_name
            assert request.node_name == caller.node_name
            assert request.test_file_path == str(tmp_path / "tests" / "test_LogicalNode.py")
            assert request.log_file_path == request.test_log == ""
            assert request.test_result is None
            assert request.amended_node_file_path == str(tmp_path / "ExampleNode.py")
            graph_path = Path(request.workflow_graph)
            assert graph_path.parent.parent == tmp_path / ".flowx" / "runtime_modifier"
            assert UUID(hex=graph_path.parent.name).hex == graph_path.parent.name
            assert json.loads(graph_path.read_text(encoding="utf-8")) == graph
        assert modifier.node_test_writer.calls == modifier.node_writer.calls == []
        assert {item.name: getattr(caller, item.name) for item in fields(caller)} == caller_snapshot
        assert all(getattr(caller, name) is value for name, value in caller_snapshot.items())
        assert caller.workflow_graph is graph
        assert caller.working_directory == workdir_input
        assert state == state_snapshot
        assert json.dumps(graph) == graph_snapshot
        assert all(native["native_visit"] == ["generate_node_test", "amend_node"] for native in native_states)
    finally:
        modifier.close()


@pytest.mark.parametrize("pool_size", [1, 2])
def test_repair_repeated_same_input_isolates_results_commands_and_log_snapshots(monkeypatch, tmp_path, pool_size):
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original\n", encoding="utf-8")
    graph = {"nodes": [{"name": "LogicalNode", "depends": []}]}
    injected = object()
    state = {"injected": injected}
    caller = NodeRuntimeModifierRequest(
        node_file_name=node_file.name, node_name="LogicalNode", workflow_graph=graph,
        working_directory=str(tmp_path), python_command="/custom/python", timeout=17, state=state,
    )
    caller.test_file_path = "stale_test.py"
    caller.log_file_path = "stale.log"
    caller.test_log = "stale failure"
    caller.test_result = {"ok": True, "command": ["stale"]}
    caller.amended_node_file_path = "stale_node.py"
    caller_snapshot = {item.name: getattr(caller, item.name) for item in fields(caller)}
    commands, run_results = [], []

    def fake_run(command, **kwargs):
        commands.append(command)
        assert command == ["/custom/python", "-m", "pytest", str(tmp_path / "tests" / "test_LogicalNode.py"), "-q"]
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["timeout"] == 17
        return SimpleNamespace(returncode=1 if len(commands) == 1 else 0, stdout=f"run {len(commands)}", stderr="")

    real_run = modifier_module.run_workflow

    def capture_run_result(engine, **kwargs):
        request = kwargs["state"]["node_runtime_modifier"]
        assert request is not caller
        assert request.state is state
        assert kwargs["state"]["injected"] is injected
        assert request.test_result is None
        assert request.test_file_path == request.log_file_path == request.test_log == ""
        assert request.amended_node_file_path == ""
        result = real_run(engine, **kwargs)
        run_results.append(result)
        return result

    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    monkeypatch.setattr(modifier_module, "run_workflow", capture_run_result)
    writer, amender = _TestWriter(), _NodeWriter()
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    engine = modifier._engine
    try:
        first = modifier.run(caller)
        first_log_snapshot = first.test_log
        second = modifier.run(caller)
        assert modifier._engine is engine
        assert first is not second
        assert first.state is second.state is state
        assert first.test_result is not second.test_result
        assert first.test_result is not caller.test_result
        assert second.test_result is not caller.test_result
        assert len(commands) == len(run_results) == len(writer.calls) == 2
        assert commands[0] is not commands[1]
        assert run_results[0].request_id != run_results[1].request_id
        for request, command, result in zip((first, second), commands, run_results):
            assert request.test_result["command"] is command
            assert request.test_result["node_name"] == "LogicalNode"
            assert request.test_file_path == str(tmp_path / "tests" / "test_LogicalNode.py")
            assert request.log_file_path == str(tmp_path / "logs" / "LogicalNode_test.log")
            assert request.amended_node_file_path == str(node_file)
            assert result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
            assert set(result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
            assert result.outputs["generate_node_test"].derived == {"test_file_path": request.test_file_path}
            assert result.outputs["run_node_test"].derived == {
                **request.test_result, "log_file_path": request.log_file_path,
            }
            assert result.outputs["run_node_test"].derived is not request.test_result
            assert result.outputs["amend_node"].derived == {"amended_node_file_path": str(node_file)}
            assert json.loads(Path(request.workflow_graph).read_text(encoding="utf-8")) == graph
        assert first.workflow_graph != second.workflow_graph
        assert first.test_result["ok"] is False
        assert second.test_result["ok"] is True
        assert first.test_log == first_log_snapshot
        assert "run 1" in first.test_log and "run 2" not in first.test_log
        assert "run 2" in second.test_log and "run 1" not in second.test_log
        assert Path(first.log_file_path).read_text(encoding="utf-8") == second.test_log
        assert amender.calls == [{
            "code_path": str(node_file), "amendment": first_log_snapshot,
            "graph_plan_path": first.workflow_graph, "current_node_name": "LogicalNode", "temperature": 0.3,
        }]
        assert node_file.read_text(encoding="utf-8") == "# amended node\n"
        first.test_result["only_first"] = True
        assert "only_first" not in second.test_result
        assert "only_first" not in caller.test_result
        assert all("only_first" not in result.outputs["run_node_test"].derived for result in run_results)
        assert {item.name: getattr(caller, item.name) for item in fields(caller)} == caller_snapshot
        assert all(getattr(caller, name) is value for name, value in caller_snapshot.items())
        assert caller.workflow_graph is graph
        assert graph == {"nodes": [{"name": "LogicalNode", "depends": []}]}
        assert state == {"injected": injected}
    finally:
        modifier.close()
    assert engine.pool.closed


def test_repair_stale_caller_result_cannot_mask_missing_run_result_or_trigger_retries(monkeypatch, tmp_path):
    caller = NodeRuntimeModifierRequest(
        node_file_name="ExampleNode.py", workflow_graph="workflow.json", working_directory=str(tmp_path),
    )
    stale_result = {"ok": True}
    caller.test_result = stale_result
    calls = []

    def completed_without_result(engine, **kwargs):
        calls.append((engine, kwargs["state"]["node_runtime_modifier"]))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(modifier_module, "run_workflow", completed_without_result)
    modifier = NodeRuntimeModifierPipeline(_TestWriter(), _NodeWriter())
    try:
        with pytest.raises(RuntimeError, match="node test execution did not produce a result for ExampleNode.py"):
            modifier.run(caller)
        assert len(calls) == 1
        engine, request = calls[0]
        assert engine is modifier._engine
        assert request is not caller
        assert request.test_result is None
        assert caller.test_result is stale_result
        assert modifier.node_test_writer.calls == modifier.node_writer.calls == []
    finally:
        modifier.close()


def test_node_runtime_modifier_stages_inherit_their_operational_components() -> None:
    assert issubclass(GenerateNodeTestFile, PromptNodeTestFileCoder)
    assert issubclass(ExecuteNodeTest, RunNodeTest)
    assert issubclass(AmendNodeFromTestLog, PromptNodeFileCoderBase)
    assert "process_input" in GenerateNodeTestFile.__dict__
    assert "process_input" in ExecuteNodeTest.__dict__
    assert "process_input" in AmendNodeFromTestLog.__dict__
    assert ExecuteNodeTest.STEP_ID == "run_node_test"
    assert ExecuteNodeTest.DEPENDENCIES == ["generate_node_test"]


def test_node_runtime_modifier_generates_runs_and_amends_from_test_log(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    test_writer = _TestWriter()
    node_writer = _NodeWriter()
    observed_command: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed_command.extend(command)
        assert kwargs["cwd"] == str(tmp_path)
        return SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="AssertionError\n")

    modifier = NodeRuntimeModifierPipeline(
        node_test_writer=test_writer,
        node_writer=node_writer,
        run_subprocess=fake_run,
        timeout_expired=TimeoutError,
    )

    request = modifier.run(
        NodeRuntimeModifierRequest(
            node_file_name="ExampleNode.py",
            workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
            working_directory=str(tmp_path),
            python_command="/custom/python",
            timeout=15,
            node_name="LogicalNode",
            test_file_name="tests/orders/test_LogicalNode.py",
            log_file_name="logs/orders/LogicalNode.log",
        )
    )

    assert modifier.json_config()["nodes"][1]["depends"] == ["generate_node_test"]
    assert test_writer.calls == [
        (str(node_file), str(tmp_path / "tests" / "orders" / "test_LogicalNode.py"))
    ]
    assert observed_command == [
        "/custom/python",
        "-m",
        "pytest",
        str(tmp_path / "tests" / "orders" / "test_LogicalNode.py"),
        "-q",
    ]
    assert request.test_result is not None
    assert request.test_result["ok"] is False
    assert "Return code: 1" in request.test_log
    assert node_writer.calls == [
        {
            "code_path": str(node_file),
            "amendment": request.test_log,
            "graph_plan_path": request.workflow_graph,
            "current_node_name": "LogicalNode",
            "temperature": 0.3,
        }
    ]
    assert Path(request.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# amended node\n"
    assert request.node_name == "LogicalNode"
    modifier.close()


def test_node_runtime_modifier_can_skip_every_stage(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    test_writer = _TestWriter()
    node_writer = _NodeWriter()

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("the test subprocess must not run")

    modifier = NodeRuntimeModifierPipeline(
        node_test_writer=test_writer,
        node_writer=node_writer,
        run_subprocess=unexpected_run,
        timeout_expired=TimeoutError,
    )

    request = modifier.run(
        NodeRuntimeModifierRequest(
            node_file_name="ExampleNode.py",
            workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
            working_directory=str(tmp_path),
            skip_generate_node_test=True,
            skip_run_node_test=True,
            skip_amend_node=True,
        )
    )

    assert request.test_file_path == str(tmp_path / "tests" / "test_ExampleNode.py")
    assert request.log_file_path == request.test_log == ""
    assert request.test_result is None
    assert test_writer.calls == []
    assert node_writer.calls == []
    assert Path(request.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# original node\n"
    modifier.close()


def test_amend_node_gates_on_user_input_only_when_amendment_is_needed(tmp_path: Path) -> None:
    amender = AmendNodeFromTestLog(client=object())
    request = NodeRuntimeModifierRequest(
        node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path),
    )
    request.test_result = {"ok": False}
    param = WorkflowParam()
    param.state = {"node_runtime_modifier": request}
    param.outputs = {"run_node_test": StepRunOutput(derived=request.test_result)}
    amender.get_workflow_param = lambda: param  # type: ignore[method-assign]

    assert AmendNodeFromTestLog.INPUT_REQUIRED is True
    assert amender.wait_function() is False
    assert param.states[amender.STEP_ID] == "awaiting_input"

    param.inputs[amender.STEP_ID] = "focus on the failing assertion"
    assert amender.wait_function() is True
    assert param.states[amender.STEP_ID] == "running"

    del param.inputs[amender.STEP_ID]
    request.test_result = {"ok": True}
    param.outputs["run_node_test"] = StepRunOutput(derived=request.test_result)
    assert amender.wait_function() is True
    assert param.states[amender.STEP_ID] == "running"

    request.test_result, request.skip_amend_node = {"ok": False}, True
    assert amender.wait_function() is True
    assert param.states[amender.STEP_ID] == "running"


def _interactive_modifier(tmp_path: Path, returncode: int):
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    writer, amender = _TestWriter(), _GatedNodeWriter(client=object())

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=returncode,
            stdout="1 passed\n" if returncode == 0 else "1 failed\n",
            stderr="" if returncode == 0 else "AssertionError: expected 2, got 1\n",
        )

    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    request = NodeRuntimeModifierRequest(
        node_file_name=node_file.name,
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
    )
    return modifier, amender, request, node_file


def test_modifier_run_interactive_asks_after_the_failed_test(tmp_path: Path) -> None:
    modifier, amender, request, node_file = _interactive_modifier(tmp_path, returncode=1)
    observed = []

    def answer(current):
        observed.append((current.test_result, current.test_log))
        return "Keep the public API; fix only the comparison."

    try:
        result = modifier.run_interactive(request, answer)
        assert observed[0][0] is result.test_result
        assert result.test_result["ok"] is False
        assert "AssertionError: expected 2, got 1" in observed[0][1]
        assert len(amender.calls) == 1
        amendment = amender.calls[0]["amendment"]
        assert observed[0][1].rstrip() in amendment
        assert "Keep the public API; fix only the comparison." in amendment
        assert "User guidance for this amendment (authoritative):" in amendment
        assert result.amended_node_file_path == str(node_file)
    finally:
        modifier.close()


def test_modifier_run_interactive_skip_leaves_the_node_unchanged(tmp_path: Path) -> None:
    modifier, amender, request, node_file = _interactive_modifier(tmp_path, returncode=1)

    try:
        result = modifier.run_interactive(
            request, lambda current: "skip" if "AssertionError" in current.test_log else "",
        )
        assert amender.calls == []
        assert result.skip_amend_node is True
        assert result.test_result is not None and result.test_result["ok"] is False
        assert result.amended_node_file_path == str(node_file)
        assert node_file.read_text(encoding="utf-8") == "# original node\n"
    finally:
        modifier.close()


def test_modifier_run_interactive_does_not_ask_when_the_test_passes(tmp_path: Path) -> None:
    modifier, amender, request, _node_file = _interactive_modifier(tmp_path, returncode=0)
    observed = []

    try:
        result = modifier.run_interactive(request, lambda current: observed.append(current) or "")
        assert observed == []
        assert amender.calls == []
        assert result.test_result is not None and result.test_result["ok"] is True
    finally:
        modifier.close()


def test_modifier_forwards_user_guidance_and_skip_to_amend_step(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    writer, amender = _TestWriter(), _NodeWriter()

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="AssertionError\n")

    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    request = NodeRuntimeModifierRequest(
        node_file_name="ExampleNode.py",
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
    )
    try:
        guided = modifier.run(request, user_input="focus on the failing assertion")
        assert len(amender.calls) == 1
        amendment = amender.calls[0]["amendment"]
        assert guided.test_log.rstrip() in amendment
        assert "focus on the failing assertion" in amendment
        assert "User guidance for this amendment (authoritative):" in amendment

        skipped = modifier.run(request, user_input="  SKIP  ")
        assert len(amender.calls) == 1
        assert skipped.amended_node_file_path == str(node_file)
        assert node_file.read_text(encoding="utf-8") == "# amended node\n"

        skipped_without_log = modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path),
                skip_generate_node_test=True, skip_run_node_test=True,
            ),
            user_input="skip",
        )
        assert skipped_without_log.test_log == skipped_without_log.log_file_path == ""
        assert skipped_without_log.amended_node_file_path == str(node_file)
        assert len(amender.calls) == 1
    finally:
        modifier.close()


def test_node_runtime_modifier_requires_existing_test_when_generation_is_disabled(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original node\n", encoding="utf-8")
    test_writer = _TestWriter()
    modifier = NodeRuntimeModifierPipeline(
        node_test_writer=test_writer,
        node_writer=_NodeWriter(),
    )

    with pytest.raises(FileNotFoundError, match="Skipped node test generation requires an existing test file"):
        modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name="ExampleNode.py",
                workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
                working_directory=str(tmp_path),
                skip_generate_node_test=True,
                skip_amend_node=True,
            )
        )

    assert test_writer.calls == []
    modifier.close()


def test_modifier_reuses_pool_without_reusing_previous_request(monkeypatch, tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original\n", encoding="utf-8")
    writer = _TestWriter()
    amender = _NodeWriter()
    commands = []

    def fake_run(command, **kwargs):
        commands.append((list(command), kwargs["timeout"]))
        return SimpleNamespace(returncode=1 if len(commands) == 1 else 0, stdout="test output", stderr="")

    run_results: list[RunResult] = []
    real_run = modifier_module.run_workflow

    def capture_run_result(engine, **kwargs):
        result = real_run(engine, **kwargs)
        run_results.append(result)
        return result

    monkeypatch.setattr(modifier_module, "run_workflow", capture_run_result)
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    engine = modifier._engine
    assert type(engine) is WorkflowEngine
    first_request = NodeRuntimeModifierRequest(
        node_file_name=node_file.name, workflow_graph={"nodes": []}, working_directory=str(tmp_path),
    )
    try:
        first = modifier.run(first_request)
        second = modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name=node_file.name, workflow_graph=first.workflow_graph, working_directory=str(tmp_path),
                skip_generate_node_test=True, timeout=7, python_command="/another/python",
                log_file_name="logs/second.log",
            )
        )
        assert len(run_results) == 2
        first_result, second_result = run_results
        assert modifier._engine is engine
        assert not hasattr(modifier, "_last_result")
        assert second_result.request_id != first_result.request_id
        assert first is not second
        assert first.test_result is not second.test_result
        assert first.test_result["ok"] is False
        assert second.test_result["ok"] is True
        for request, result in ((first, first_result), (second, second_result)):
            assert result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
            assert set(result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
            assert result.outputs["generate_node_test"].derived == {"test_file_path": request.test_file_path}
            assert result.outputs["run_node_test"].derived == {
                **request.test_result, "log_file_path": request.log_file_path,
            }
            assert result.outputs["amend_node"].derived == {"amended_node_file_path": str(node_file)}
        assert len(writer.calls) == len(amender.calls) == 1
        assert commands[1][0][0] == "/another/python"
        assert commands[1][1] == 7
        assert second.log_file_path.endswith("logs/second.log")
        assert first_result.outputs["run_node_test"].derived["ok"] is False
        assert second_result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
    finally:
        modifier.close()
    assert modifier._engine is None
    assert engine.pool.closed
    modifier.close()
    with pytest.raises(RuntimeError, match="closed"):
        modifier.run(first_request)


@pytest.mark.parametrize("pool_size", [1, 2])
def test_modifier_never_allocates_native_test_runners_after_eager_construction(monkeypatch, tmp_path, pool_size):
    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    barrier = Barrier(2) if pool_size == 2 else None
    commands, allocations, run_results = [], [], []

    real_run = modifier_module.run_workflow

    def capture_run_result(engine, **kwargs):
        result = real_run(engine, **kwargs)
        run_results.append(result)
        return result

    monkeypatch.setattr(modifier_module, "run_workflow", capture_run_result)

    def fake_run(command, **kwargs):
        name = Path(kwargs["cwd"]).name
        assert command[0] == f"/python/{name}"
        assert command[-2] == str(tmp_path / name / "tests" / f"test_{name}.py")
        commands.append(command)
        if barrier is not None and name != "Third":
            barrier.wait(timeout=10)
        return SimpleNamespace(returncode=0, stdout=f"passed {name}", stderr="")

    writer, amender = _TestWriter(), _NodeWriter()
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    engine = modifier._engine
    original_init = RunNodeTest.__init__

    def record_native_allocation(self, **kwargs):
        allocations.append((type(self).__name__, kwargs["node_name"]))
        original_init(self, **kwargs)

    # All native pool slots already exist. Any subsequent constructor call is
    # an unregistered request-time native allocation, not eager pool setup.
    monkeypatch.setattr(RunNodeTest, "__init__", record_native_allocation)

    def request(name):
        workdir = tmp_path / name
        workdir.mkdir()
        (workdir / f"{name}.py").write_text(f"# {name}\n", encoding="utf-8")
        return NodeRuntimeModifierRequest(
            node_file_name=f"{name}.py", node_name=name, working_directory=str(workdir),
            workflow_graph={}, python_command=f"/python/{name}",
        )

    try:
        assert type(engine) is WorkflowEngine
        assert allocations == []
        first, second = request("First"), request("Second")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(modifier.run, item) for item in (first, second)]
            requests = [future.result(timeout=15) for future in futures]
        requests.append(modifier.run(request("Third")))
        assert modifier._engine is engine
        assert len(commands) == len(run_results) == len(writer.calls) == 3
        assert amender.calls == []
        assert len({id(command) for command in commands}) == 3
        assert len({id(item.test_result) for item in requests}) == 3
        for result in run_results:
            assert result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
            assert set(result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
        for item in requests:
            assert any(item.test_result["command"] is command for command in commands)
            assert item.test_result["ok"] is True
            assert f"passed {item.node_name}" in item.test_log
            assert Path(item.test_file_path).is_file()
            assert item.amended_node_file_path == str(tmp_path / item.node_name / f"{item.node_name}.py")
            assert Path(item.amended_node_file_path).read_text(encoding="utf-8") == f"# {item.node_name}\n"
            result = next(result for result in run_results if result.outputs["run_node_test"].derived["node_name"] == item.node_name)
            assert result.outputs["generate_node_test"].derived == {"test_file_path": item.test_file_path}
            assert result.outputs["run_node_test"].derived == {**item.test_result, "log_file_path": item.log_file_path}
            assert result.outputs["amend_node"].derived == {"amended_node_file_path": item.amended_node_file_path}
        assert allocations == [], f"request-time native RunNodeTest allocations: {allocations}"
    finally:
        modifier.close()
    assert engine.pool.closed


def test_modifier_can_amend_using_an_existing_log(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")
    log = tmp_path / "existing.log"
    log.write_text("AssertionError: wrong value", encoding="utf-8")
    writer, amender = _TestWriter(), _NodeWriter()
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=lambda *_a, **_kw: pytest.fail("subprocess"))
    try:
        request = modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
                skip_generate_node_test=True, skip_run_node_test=True, log_file_name=log.name,
            )
        )
        assert request.test_result is None
        assert request.test_log == log.read_text(encoding="utf-8")
        assert request.log_file_path == str(log)
        assert request.test_file_path == str(tmp_path / "tests" / "test_ExampleNode.py")
        assert writer.calls == []
        assert amender.calls == [{
            "code_path": str(tmp_path / "ExampleNode.py"), "amendment": request.test_log,
            "graph_plan_path": request.workflow_graph, "current_node_name": "ExampleNode", "temperature": 0.3,
        }]
        assert request.amended_node_file_path == str(tmp_path / "ExampleNode.py")
        assert Path(request.amended_node_file_path).read_text(encoding="utf-8") == "# amended node\n"
    finally:
        modifier.close()


def test_missing_log_fails_natively_without_running_amendment(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")
    amender = _NodeWriter()
    modifier = NodeRuntimeModifierPipeline(_TestWriter(), amender)
    try:
        with pytest.raises(RuntimeError, match="run_node_test.*existing log file"):
            modifier.run(
                NodeRuntimeModifierRequest(
                    node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
                    skip_generate_node_test=True, skip_run_node_test=True,
                )
            )
        assert amender.calls == []
        assert not hasattr(modifier, "_last_result")
    finally:
        modifier.close()


def test_generation_failure_never_falls_back_to_direct_stage_calls(tmp_path: Path) -> None:
    calls = []

    @dataclass
    class BrokenWriter(_TestWriter):
        events: list = field(default_factory=list)

        def write_test_from_node_file(self, *args, **kwargs):
            self.events.append("generate")
            raise ValueError("generation failed")

    modifier = NodeRuntimeModifierPipeline(
        BrokenWriter(events=calls), _NodeWriter(), run_subprocess=lambda *_a, **_kw: pytest.fail("subprocess"),
    )
    try:
        with pytest.raises(RuntimeError, match="generate_node_test.*generation failed"):
            modifier.run(
                NodeRuntimeModifierRequest(
                    node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
                )
            )
        assert calls == ["generate"]
        assert modifier.node_writer.calls == []
    finally:
        modifier.close()


def test_modifier_executes_the_actual_normalized_generated_test_path(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")

    class NormalizingWriter(_TestWriter):
        def write_test_from_node_file(self, node_file_path, output_path, **kwargs):
            return super().write_test_from_node_file(node_file_path, str(Path(output_path).with_suffix(".py")), **kwargs)

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="1 passed", stderr="")

    modifier = NodeRuntimeModifierPipeline(NormalizingWriter(), _NodeWriter(), run_subprocess=fake_run)
    try:
        request = modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path),
                test_file_name="tests/test_example.txt",
            )
        )
        assert commands[0][-2] == request.test_file_path
        assert request.test_file_path.endswith("test_example.py")
        assert modifier.node_writer.calls == []
    finally:
        modifier.close()


def test_modifier_runs_real_pytest_and_skips_amendment_on_success(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")
    modifier = NodeRuntimeModifierPipeline(_TestWriter(), _NodeWriter())
    try:
        request = modifier.run(
            NodeRuntimeModifierRequest(
                node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path),
            )
        )
        assert request.test_result["returncode"] == 0
        assert request.test_result["ok"] is True
        assert "1 passed" in request.test_log
        assert modifier.node_test_writer.calls == [
            (str(tmp_path / "ExampleNode.py"), str(tmp_path / "tests" / "test_ExampleNode.py")),
        ]
        assert modifier.node_writer.calls == []
        assert request.amended_node_file_path == str(tmp_path / "ExampleNode.py")
        assert Path(request.amended_node_file_path).read_text(encoding="utf-8") == "# original\n"
    finally:
        modifier.close()


@pytest.mark.parametrize("pool_size", [1, 2])
def test_modifier_constructs_operational_nodes_and_isolates_concurrent_requests(monkeypatch, tmp_path, pool_size):
    writers, amenders, executions, configs, run_results = [], [], [], [], []
    pooled_executors = []
    pooled_commands = {}
    barrier = Barrier(2) if pool_size == 2 else None

    @dataclass
    class Writer(_TestWriter):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Writer
            assert self.STEP_ID == "generate_node_test"
            assert self.DEPENDENCIES == []
            assert self.get_workflow_param().state is session_state
            assert "previous_request" not in session_state
            session_state["previous_request"] = True
            return super().process_input(user_input, dependency_results, session_state)

        def write_test_from_node_file(self, node_file_path, output_path, **kwargs):
            request = self.get_workflow_param().state["node_runtime_modifier"]
            assert kwargs == {"temperature": request.test_temperature}
            return super().write_test_from_node_file(node_file_path, output_path, **kwargs)

    @dataclass
    class Amender(_NodeWriter):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Amender
            assert self.STEP_ID == "amend_node"
            assert self.DEPENDENCIES == ["run_node_test"]
            assert set(dependency_results) == {"run_node_test"}
            assert self.get_workflow_param().state is session_state
            assert dependency_results["run_node_test"].derived["node_name"] == session_state["node_runtime_modifier"].node_name
            return super().process_input(user_input, dependency_results, session_state)

    def fake_run(command, **kwargs):
        # The plain function owns execution locals; the enclosing native step
        # retains only eager construction fields and execution dependencies.
        execution_frame = inspect.currentframe().f_back
        assert execution_frame.f_code is _execute_node_test.__code__
        execution = execution_frame.f_locals
        assert "self" not in execution
        step_locals = execution_frame.f_back.f_locals
        request = step_locals["request"]
        pooled_step = step_locals["self"]
        pooled_executors.append(pooled_step)
        _assert_executor_pristine(pooled_step, fake_run, TimeoutError)
        original_command = pooled_commands.setdefault(id(pooled_step), pooled_step.command)
        assert pooled_step.command is original_command
        assert pooled_step.get_workflow_param().state is step_locals["session_state"]
        assert pooled_step.get_workflow_param().state["node_runtime_modifier"] is request
        assert execution["run_subprocess"] is fake_run
        assert execution["timeout_expired"] is TimeoutError
        assert execution["command"] is command
        assert execution["node_name"] == request.node_name
        assert str(execution["test_path"]) == request.test_file_path
        assert execution["log_path"] == tmp_path / "logs" / f"{request.node_name}.log"
        assert execution["cwd"] == kwargs["cwd"] == request.working_directory
        assert execution["timeout"] == kwargs["timeout"] == request.timeout
        assert command[-2] == request.test_file_path
        assert command[0] == request.python_command
        assert request.test_result is None
        executions.append((execution, command, request))
        if barrier is not None and request.node_name != "Third":
            barrier.wait(timeout=10)
        return SimpleNamespace(returncode=1, stdout=f"failure for {request.node_name}", stderr="")

    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    real_init = modifier_module.init_workflow_engine

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_init(node_classes, workflow_config)

    real_run = modifier_module.run_workflow

    def capture_run_result(engine, **kwargs):
        result = real_run(engine, **kwargs)
        run_results.append(result)
        return result

    monkeypatch.setattr(modifier_module, "init_workflow_engine", capture_config)
    monkeypatch.setattr(modifier_module, "run_workflow", capture_run_result)
    writer, amender = Writer(instances=writers), Amender(instances=amenders)
    writers.clear()
    amenders.clear()
    original_metadata = [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Writer, ExecuteNodeTest, Amender)]
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run, timeout_expired=TimeoutError)
    engine = modifier._engine

    def request(name, timeout):
        (tmp_path / f"{name}.py").write_text(f"# original {name}", encoding="utf-8")
        return NodeRuntimeModifierRequest(
            node_file_name=f"{name}.py", node_name=name, working_directory=str(tmp_path),
            workflow_graph={"name": name}, test_file_name=f"tests/{name}/test_node.py",
            log_file_name=f"logs/{name}.log", python_command=f"/python/{name}", timeout=timeout,
            test_temperature=timeout / 100, amendment_temperature=timeout / 50,
        )

    try:
        assert type(engine) is WorkflowEngine
        assert len(configs) == 1
        component_classes = {"generate_node_test": Writer, "run_node_test": ExecuteNodeTest, "amend_node": Amender}
        assert configs[0][0] == component_classes
        assert list(configs[0][0]) == list(component_classes)
        assert all(configs[0][0][name] is cls for name, cls in component_classes.items())
        assert configs[0][1] is config
        assert configs[0][1]["pool_size"] == pool_size
        assert {node["name"]: node.get("depends", []) for node in configs[0][1]["nodes"]} == {
            "generate_node_test": [], "run_node_test": ["generate_node_test"], "amend_node": ["run_node_test"],
        }
        assert engine.steps_meta == [cls.step_meta() for cls in component_classes.values()]
        for instances, prototype, cls in ((writers, writer, Writer), (amenders, amender, Amender)):
            assert len(instances) == pool_size
            assert len({id(node) for node in instances}) == pool_size
            assert all(type(node) is cls and node is not prototype for node in instances)
            assert all("process_input" not in node.__dict__ for node in instances)
            assert all(node.instances is instances and node.calls is prototype.calls for node in instances)
        writer.process_input = amender.process_input = lambda *_a, **_kw: pytest.fail("prototype dispatch")
        writer.write_test_from_node_file = lambda *_a, **_kw: pytest.fail("prototype test generation")
        amender.amend_code_with_feedback = lambda *_a, **_kw: pytest.fail("prototype amendment")
        modifier.node_test_writer = _TestWriter()
        modifier.node_writer = _NodeWriter()
        modifier.run_subprocess = lambda *_a, **_kw: pytest.fail("late dependency replacement")
        modifier.timeout_expired = RuntimeError
        first, second = request("First", 11), request("Second", 23)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(modifier.run, request_input) for request_input in (first, second)]
            requests = [future.result(timeout=15) for future in futures]
        assert len(executions) == 2
        assert len({id(execution) for execution, _, _ in executions}) == 2
        assert len({id(node) for node in pooled_executors}) == pool_size
        for node in pooled_executors:
            _assert_executor_pristine(node, fake_run, TimeoutError)
            assert node.command is pooled_commands[id(node)]
        assert executions[0][1] is not executions[1][1]
        first_commands = [list(item.test_result["command"]) for item in requests]
        third = modifier.run(request("Third", 31))
        assert len(executions) == 3
        assert len({id(execution) for execution, _, _ in executions}) == 3
        assert len({id(node) for node in pooled_executors}) == pool_size
        for node in pooled_executors:
            _assert_executor_pristine(node, fake_run, TimeoutError)
            assert node.command is pooled_commands[id(node)]
        assert modifier._engine is engine
        assert len(writers) == len(amenders) == pool_size
        assert requests[0].test_result is not requests[1].test_result
        assert third.test_result is not requests[0].test_result
        assert third.test_result is not requests[1].test_result
        assert len({id(command) for _, command, _ in executions}) == 3
        assert all(item.test_result["command"] is command for _, command, item in executions)
        assert all(execution["command"] is item.test_result["command"] for execution, _, item in executions)
        assert [item.test_result["command"] for item in requests] == first_commands
        assert len(run_results) == 3
        assert len({result.request_id for result in run_results}) == 3
        for result in run_results:
            assert result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
            assert set(result.outputs) == {"generate_node_test", "run_node_test", "amend_node"}
        for item in [*requests, third]:
            assert f"failure for {item.node_name}" in item.test_log
            assert f"logs/{item.node_name}.log" in item.log_file_path
            assert item.test_result["ok"] is False
            assert item.test_result["node_name"] == item.node_name
            assert item.test_temperature == item.timeout / 100
            assert item.amendment_temperature == item.timeout / 50
            assert Path(item.amended_node_file_path).name == f"{item.node_name}.py"
            assert Path(item.amended_node_file_path).read_text(encoding="utf-8") == "# amended node\n"
            assert Path(item.test_file_path).is_file()
            result = next(result for result in run_results if result.outputs["run_node_test"].derived["node_name"] == item.node_name)
            assert result.outputs["generate_node_test"].derived == {"test_file_path": item.test_file_path}
            assert result.outputs["run_node_test"].derived == {**item.test_result, "log_file_path": item.log_file_path}
            assert result.outputs["amend_node"].derived == {"amended_node_file_path": item.amended_node_file_path}
        assert len({item.workflow_graph for item in [*requests, third]}) == 3
        assert {call["current_node_name"]: call["amendment"] for call in amender.calls} == {
            item.node_name: item.test_log for item in [*requests, third]
        }
        assert {call["current_node_name"]: call["temperature"] for call in amender.calls} == {
            item.node_name: item.amendment_temperature for item in [*requests, third]
        }
        assert [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Writer, ExecuteNodeTest, Amender)] == original_metadata
    finally:
        modifier.close()
    assert modifier._engine is None
    assert engine.pool.closed


@pytest.mark.parametrize("pool_size", [1, 2])
def test_modifier_retains_dataclass_settings_and_restores_construction_scope(monkeypatch, pool_size):
    @dataclass
    class Writer(GenerateNodeTestFile):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    @dataclass
    class Amender(AmendNodeFromTestLog):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    modifiers, engines = [], []
    try:
        for label in ("first", "second"):
            writers, amenders = [], []
            writer = Writer(
                client=object(), instances=writers, system_prompt=f"{label} test prompt",
                session_marking_prompt=f"{label} test marking", root_dir_path=f"/{label}/root",
                timeout=4.5, use_streaming=False,
            )
            amender = Amender(
                client=object(), instances=amenders, system_prompt=f"{label} amendment prompt",
                session_marking_prompt=f"{label} amendment marking", timeout=9.5, use_streaming=False,
            )
            writers.clear()
            amenders.clear()
            with node_settings({Writer.STEP_ID: {"system_prompt": "outer prompt"}}):
                modifier = NodeRuntimeModifierPipeline(writer, amender)
                modifiers.append(modifier)
                engines.append(modifier._engine)
                assert type(engines[-1]) is WorkflowEngine
                assert _TestWriter().system_prompt == "outer prompt"
            assert _TestWriter().system_prompt == GenerateNodeTestFile.system_prompt
            for prototype, instances in ((writer, writers), (amender, amenders)):
                assert len(instances) == pool_size
                assert len({id(node) for node in instances}) == pool_size
                assert all(type(node) is type(prototype) and node is not prototype for node in instances)
                assert all(node.instances is instances for node in instances)
                assert all(node.client is prototype.client for node in instances)
                assert all(node.system_prompt == prototype.system_prompt for node in instances)
                assert all(node.system_prompt.count(prototype.session_marking_prompt) == 1 for node in instances)
                assert all(node.timeout == prototype.timeout and node.use_streaming is False for node in instances)
                prompt_snapshot = prototype.system_prompt
                prototype.system_prompt = "late replacement"
                prototype.client = object()
                assert all(node.system_prompt == prompt_snapshot for node in instances)
                assert all(node.client is not prototype.client for node in instances)
            assert all(node.root_dir_path == writer.root_dir_path for node in writers)
            assert modifier._engine.steps_meta == [node.step_meta() for node in (Writer, ExecuteNodeTest, Amender)]
        assert modifiers[0]._engine is not modifiers[1]._engine
    finally:
        for modifier in modifiers:
            modifier.close()
    assert all(modifier._engine is None for modifier in modifiers)
    assert all(engine.pool.closed for engine in engines)


def test_execute_node_test_resets_timeout_exception_results_commands_and_skip_state(tmp_path):
    commands = []
    executions = []

    def run(command, **kwargs):
        execution_frame = inspect.currentframe().f_back
        assert execution_frame.f_code is _execute_node_test.__code__
        execution = execution_frame.f_locals
        assert "self" not in execution
        assert execution["command"] is command
        assert execution["timeout"] == kwargs["timeout"]
        assert execution["cwd"] == kwargs["cwd"] == str(tmp_path)
        assert execution["run_subprocess"] is run
        assert execution["timeout_expired"] is TimeoutError
        assert execution_frame.f_back.f_locals["request"].test_result is None
        _assert_executor_pristine(execution_frame.f_back.f_locals["self"], run, TimeoutError)
        executions.append(execution)
        commands.append(command)
        if len(commands) == 1:
            raise TimeoutError()
        if len(commands) == 2:
            raise ValueError("broken subprocess")
        return SimpleNamespace(returncode=0, stdout="fresh output", stderr="")

    runner = ExecuteNodeTest(run_subprocess=run, timeout_expired=TimeoutError)
    original_command = runner.command
    requests, outputs = [], []
    for name, timeout, skip in (("First", 2, False), ("Second", 9, False), ("Third", 12, False), ("Skipped", 4, True)):
        request = NodeRuntimeModifierRequest(
            node_file_name=f"{name}.py", node_name=name, working_directory=str(tmp_path), python_command=f"/python/{name}",
            workflow_graph={}, timeout=timeout, skip_run_node_test=skip, skip_amend_node=True,
        )
        if skip:
            request.test_result = requests[2].test_result
            request.test_log = requests[2].test_log
            request.log_file_path = requests[2].log_file_path
        outputs.append(runner.process_input("", {}, {"node_runtime_modifier": request}))
        requests.append(request)
        _assert_executor_pristine(runner, run, TimeoutError)
        assert runner.command is original_command
    assert len(commands) == len(executions) == 3
    assert len({id(command) for command in commands}) == 3
    assert len({id(execution) for execution in executions}) == 3
    assert len({id(request.test_result) for request in requests[:3]}) == 3
    for execution, command, request, output in zip(executions, commands, requests, outputs):
        assert execution["command"] is request.test_result["command"] is command
        assert output.derived["command"] is command
        assert execution["node_name"] == request.node_name
        assert str(execution["test_path"]) == request.test_file_path
        assert str(execution["log_path"]) == request.log_file_path
        assert output.derived == {**request.test_result, "log_file_path": request.log_file_path}
        assert output.derived is not request.test_result
    assert requests[0].test_result["timed_out"] is True
    assert requests[0].test_result["ok"] is False
    assert requests[1].test_result["timed_out"] is False
    assert requests[1].test_result["ok"] is False
    assert requests[1].test_result["stderr"] == "ValueError: broken subprocess"
    assert requests[2].test_result["timed_out"] is False
    assert requests[2].test_result["ok"] is True
    assert requests[2].test_result["stderr"] == ""
    assert "after 2 seconds" in requests[0].test_log
    assert "ValueError: broken subprocess" in requests[1].test_log
    assert "fresh output" in requests[2].test_log
    assert requests[3].test_result is None
    assert requests[3].test_log == requests[3].log_file_path == ""
    assert outputs[3].derived == {"log_file_path": ""}
    assert commands[0][0] == "/python/First"
    assert ExecuteNodeTest().command == []