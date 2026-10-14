"""Modified: cover exact class maps, scoped settings, real repair nodes and concurrency."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from ag_ui_workflow import WorkflowEngine, WorkflowStepNode

from flowx_core.runtime import RunNodeTest
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    ExecuteNodeTest,
    GenerateNodeTestFile,
    NodeRuntimeModifierContext,
    NodeRuntimeModifierPipeline,
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

    context = modifier.run(
        node_file_name="ExampleNode.py",
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
        python_command="/custom/python",
        timeout=15,
        workflow_name="orders",
        node_name="LogicalNode",
        test_file_name="tests/orders/test_LogicalNode.py",
        log_file_name="logs/orders/LogicalNode.log",
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
    assert context.test_result is not None
    assert context.test_result["ok"] is False
    assert "Return code: 1" in context.test_log
    assert node_writer.calls == [
        {
            "code_path": str(node_file),
            "amendment": context.test_log,
            "graph_plan_path": context.workflow_graph,
            "current_node_name": "LogicalNode",
            "temperature": 0.3,
        }
    ]
    assert Path(context.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# amended node\n"
    assert context.workflow_name == "orders"
    assert context.node_name == "LogicalNode"
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

    context = modifier.run(
        node_file_name="ExampleNode.py",
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
        skip_generate_node_test=True,
        skip_run_node_test=True,
        skip_amend_node=True,
    )

    assert context.skipped_stages == ["generate_node_test", "run_node_test", "amend_node"]
    assert test_writer.calls == []
    assert node_writer.calls == []
    assert Path(context.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# original node\n"
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
            node_file_name="ExampleNode.py",
            workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
            working_directory=str(tmp_path),
            skip_generate_node_test=True,
            skip_amend_node=True,
        )

    assert test_writer.calls == []
    modifier.close()


def test_modifier_reuses_pool_without_reusing_previous_context(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original\n", encoding="utf-8")
    writer = _TestWriter()
    amender = _NodeWriter()
    commands = []

    def fake_run(command, **kwargs):
        commands.append((list(command), kwargs["timeout"]))
        return SimpleNamespace(returncode=1 if len(commands) == 1 else 0, stdout="test output", stderr="")

    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run)
    try:
        first = modifier.run(
            node_file_name=node_file.name, workflow_graph={"nodes": []}, working_directory=str(tmp_path),
        )
        engine = modifier._engine
        first_result = modifier._last_result
        second = modifier.run(
            node_file_name=node_file.name, workflow_graph=first.workflow_graph, working_directory=str(tmp_path),
            skip_generate_node_test=True, timeout=7, python_command="/another/python",
            log_file_name="logs/second.log",
        )
        assert modifier._engine is engine
        assert modifier._last_result.request_id != first_result.request_id
        assert first.test_result["ok"] is False
        assert second.test_result["ok"] is True
        assert first.skipped_stages == []
        assert second.skipped_stages == ["generate_node_test", "amend_node"]
        assert first.completed_stages == second.completed_stages == {
            "generate_node_test", "run_node_test", "amend_node",
        }
        assert len(writer.calls) == len(amender.calls) == 1
        assert commands[1][0][0] == "/another/python"
        assert commands[1][1] == 7
        assert second.log_file_path.endswith("logs/second.log")
        assert first_result.outputs["run_node_test"].derived["ok"] is False
        assert modifier._last_result.new_steps == ["generate_node_test", "run_node_test", "amend_node"]
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
        context = modifier.run(
            node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
            skip_generate_node_test=True, skip_run_node_test=True, log_file_name=log.name,
        )
        assert context.test_result is None
        assert context.test_log == log.read_text(encoding="utf-8")
        assert context.skipped_stages == ["generate_node_test", "run_node_test"]
        assert writer.calls == []
        assert amender.calls[0]["amendment"] == context.test_log
    finally:
        modifier.close()


def test_missing_log_fails_natively_without_running_amendment(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")
    amender = _NodeWriter()
    modifier = NodeRuntimeModifierPipeline(_TestWriter(), amender)
    try:
        with pytest.raises(RuntimeError, match="run_node_test.*existing log file"):
            modifier.run(
                node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
                skip_generate_node_test=True, skip_run_node_test=True,
            )
        assert amender.calls == []
        assert modifier._last_result is None
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
                node_file_name="ExampleNode.py", workflow_graph={"nodes": []}, working_directory=str(tmp_path),
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
        context = modifier.run(
            node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path),
            test_file_name="tests/test_example.txt",
        )
        assert commands[0][-2] == context.test_file_path
        assert context.test_file_path.endswith("test_example.py")
        assert modifier.node_writer.calls == []
    finally:
        modifier.close()


def test_modifier_runs_real_pytest_and_skips_amendment_on_success(tmp_path: Path) -> None:
    (tmp_path / "ExampleNode.py").write_text("# original\n", encoding="utf-8")
    modifier = NodeRuntimeModifierPipeline(_TestWriter(), _NodeWriter())
    try:
        context = modifier.run(node_file_name="ExampleNode.py", workflow_graph={}, working_directory=str(tmp_path))
        assert context.test_result["returncode"] == 0
        assert context.test_result["ok"] is True
        assert "1 passed" in context.test_log
        assert context.skipped_stages == ["amend_node"]
        assert modifier.node_writer.calls == []
    finally:
        modifier.close()


@pytest.mark.parametrize("pool_size", [1, 2])
def test_modifier_constructs_operational_nodes_and_isolates_concurrent_requests(monkeypatch, tmp_path, pool_size):
    writers, amenders, executions, configs = [], [], [], []
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
        # Observe the inherited operational executor at the injected subprocess
        # boundary: it must be the attached native node, never a temporary runner.
        runner = inspect.currentframe().f_back.f_locals["self"]
        assert type(runner) is ExecuteNodeTest
        assert runner.STEP_ID == "run_node_test"
        assert runner.DEPENDENCIES == ["generate_node_test"]
        assert "process_input" not in runner.__dict__
        assert runner.run_subprocess is fake_run
        assert runner.timeout_expired is TimeoutError
        param = runner.get_workflow_param()
        context = param.state["node_runtime_modifier"]
        assert runner.result is None
        assert command[-2] == context.test_file_path
        assert command[0] == context.python_command
        assert kwargs["timeout"] == context.timeout
        executions.append((runner, command, context))
        if barrier is not None and context.node_name != "Third":
            barrier.wait(timeout=10)
        return SimpleNamespace(returncode=1, stdout=f"failure for {context.node_name}", stderr="")

    config = NodeRuntimeModifierPipeline.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(NodeRuntimeModifierPipeline, "json_config", staticmethod(lambda: config))
    real_init = modifier_module.init_workflow_engine

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_init(node_classes, workflow_config)

    monkeypatch.setattr(modifier_module, "init_workflow_engine", capture_config)
    writer, amender = Writer(instances=writers), Amender(instances=amenders)
    writers.clear()
    amenders.clear()
    original_metadata = [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Writer, ExecuteNodeTest, Amender)]
    modifier = NodeRuntimeModifierPipeline(writer, amender, run_subprocess=fake_run, timeout_expired=TimeoutError)
    engine = modifier._engine

    def request(name, timeout):
        (tmp_path / f"{name}.py").write_text(f"# original {name}", encoding="utf-8")
        return dict(
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
            futures = [executor.submit(modifier.run, **kwargs) for kwargs in (first, second)]
            contexts = [future.result(timeout=15) for future in futures]
        assert len(executions) == 2
        assert len({id(runner) for runner, _, _ in executions}) == pool_size
        assert executions[0][1] is not executions[1][1]
        first_commands = [list(context.test_result["command"]) for context in contexts]
        third = modifier.run(**request("Third", 31))
        assert len(executions) == 3
        assert len({id(runner) for runner, _, _ in executions}) == pool_size
        assert modifier._engine is engine
        assert len(writers) == len(amenders) == pool_size
        assert contexts[0].test_result is not contexts[1].test_result
        assert third.test_result is not contexts[0].test_result
        assert third.test_result is not contexts[1].test_result
        assert len({id(command) for _, command, _ in executions}) == 3
        assert all(context.test_result["command"] is command for _, command, context in executions)
        assert [context.test_result["command"] for context in contexts] == first_commands
        for context in [*contexts, third]:
            assert f"failure for {context.node_name}" in context.test_log
            assert f"logs/{context.node_name}.log" in context.log_file_path
            assert context.completed_stages == {"generate_node_test", "run_node_test", "amend_node"}
            assert context.skipped_stages == []
            assert context.test_result["node_name"] == context.node_name
            assert Path(context.amended_node_file_path).name == f"{context.node_name}.py"
        assert len({context.workflow_graph for context in [*contexts, third]}) == 3
        assert {call["current_node_name"]: call["amendment"] for call in amender.calls} == {
            context.node_name: context.test_log for context in [*contexts, third]
        }
        assert [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Writer, ExecuteNodeTest, Amender)] == original_metadata
    finally:
        modifier.close()
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
    modifiers = []
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
    assert all(modifier._engine.pool.closed for modifier in modifiers)


def test_execute_node_test_resets_timeout_results_commands_and_skip_state(tmp_path):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            raise TimeoutError()
        return SimpleNamespace(returncode=0, stdout="fresh output", stderr="")

    runner = ExecuteNodeTest(run_subprocess=run, timeout_expired=TimeoutError)
    contexts, outputs = [], []
    for name, timeout, skip in (("First", 2, False), ("Second", 9, False), ("Skipped", 4, True)):
        context = NodeRuntimeModifierContext(
            node_file_name=f"{name}.py", working_directory=str(tmp_path), python_command=f"/python/{name}",
            timeout=timeout, skip_run_node_test=skip, skip_amend_node=True,
        )
        if skip:
            context.test_result = contexts[1].test_result
            context.test_log = contexts[1].test_log
            context.log_file_path = contexts[1].log_file_path
        outputs.append(runner.process_input("", {}, {"node_runtime_modifier": context}))
        contexts.append(context)
    assert len(commands) == 2
    assert commands[0] is not commands[1]
    assert contexts[0].test_result["timed_out"] is True
    assert contexts[1].test_result["timed_out"] is False
    assert "after 2 seconds" in contexts[0].test_log
    assert "fresh output" in contexts[1].test_log
    assert contexts[2].test_result is None
    assert contexts[2].test_log == contexts[2].log_file_path == ""
    assert contexts[2].skipped_stages == ["run_node_test"]
    assert runner.result is None
    assert runner.command[0] == "/python/Skipped"
    assert outputs[2].derived == {"log_file_path": ""}
    assert commands[0][0] == "/python/First"
    assert outputs[0].derived["command"] is commands[0]
    assert outputs[1].derived["command"] is commands[1]
    assert ExecuteNodeTest().command == []