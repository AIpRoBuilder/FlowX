"""Modified 2026-10-14: construct services from explicit dependencies without a builder.

Modified 2026-10-14: verify graph-workflow request delegation and shared artifact state.
Modified 2026-10-14: test eager workflow ownership and builder-owned request publication.
Modified 2026-10-15: test unified generation requests and caller-isolated mutable repair outputs.
Modified 2026-10-15: verify cleaned request APIs, builder-local routing and deterministic main payloads.
Modified 2026-10-15: migrate generation coverage to complete per-node requests without batch/callback state.
Modified 2026-10-15: replace removed batch coverage with immediate rejection and direct graph refresh tests.
Modified 2026-10-16: drop the removed workflow service wrappers; AgentBuilder owns its four workflows.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import MISSING, FrozenInstanceError, fields
from inspect import Parameter, signature
from itertools import product
import json
from pathlib import Path
from threading import RLock
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, call

import pytest
from ag_ui_workflow import StepRunOutput, WorkflowStepNode

from flowx_core import agent_builder as agent_builder_module, builder_services
from flowx_core.agent_builder import AgentBuilder
from flowx_core.architect.graph import NodeMeta
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.builder_support import BuildSession, WorkflowArtifactState
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows import main_entrypoint_generation as main_entrypoint_module
from flowx_core.workflows.graph_generation import GraphGenerationRequest
from flowx_core.workflows.main_entrypoint_generation import MainEntrypointGenerationRequest
from flowx_core.workflows.node_generation import (
    NodeGenerationRequest,
    NodeGenerationWorkflow,
)
from flowx_core.workflows.node_runtime_modifier import NodeRuntimeModifierRequest


_WORKFLOW_CASES = (
    (
        "graph", "GraphGenerationWorkflow",
        ("analyzer", "planner", "graph_auditor"), "_graph_workflow",
    ),
    (
        "node", "NodeGenerationWorkflow",
        ("node_planner", "node_coder", "node_auditor"), "_node_workflow",
    ),
    (
        "artifact", "NodeRuntimeModifierPipeline",
        ("node_test_writer", "node_writer", "run_subprocess", "timeout_expired"),
        "_modifier_pipeline",
    ),
    (
        "main", "MainEntrypointGenerationWorkflow",
        ("main_writer", "main_entry_auditor"), "_main_workflow",
    ),
)
_RUNTIME_DEPENDENCIES = (
    "logger", "select_python_command", "popen_subprocess", "run_subprocess", "timeout_expired",
)
_SERVICE_ATTRIBUTES = tuple(case[3] for case in _WORKFLOW_CASES) + ("_runtime_service",)
_LEGACY_SERVICE_ATTRIBUTES = (
    "builder", "session", "build_session", "_session", "request", "requests", "_last_request",
    "generation_contexts", "workflows", "contexts", "modifiers", "_cache_lock",
    "_workflow_for", "get_or_create_modifier", "get_or_create_context",
    "analyze_requirement", "plan_graph", "amend_graph", "generate_nodes", "run_node_modifier",
    "make_node_coder", "instantiate_graph", "validate_generated_artifacts",
    "resolve_amendment_target", "output_auditor",
)
_REMOVED_NODE_GENERATION_FIELDS = {
    "context", "workflow_name", "node_name", "node_locations", "skipped_node_names",
    "node_names", "selected_node_names", "node_docs_dir", "generate_markdowns",
    "markdown_amendments", "node_amendments", "node_formats", "raw_nodes", "artifacts",
    "workflow_requests", "workflow_registry", "workflow_outputs",
}


def _assert_complete_node_request(request) -> None:
    assert isinstance(request, NodeGenerationRequest)
    assert isinstance(request.node_meta, NodeMeta)
    assert request.raw_node["name"] == request.node_meta.name
    assert isinstance(request.target_path, Path)
    assert isinstance(request.markdown_path, Path)
    assert isinstance(request.skip_node_planner, bool)
    for name in ("graph_plan_path", "requirement_md_path", "requirement_text", "dependency_context"):
        assert isinstance(getattr(request, name), str)
    assert set(vars(request)) == {field.name for field in fields(NodeGenerationRequest)}
    for name in _REMOVED_NODE_GENERATION_FIELDS:
        assert not hasattr(request, name), name


def _service_dependencies(names):
    dependencies = {
        name: TimeoutError if name == "timeout_expired" else Mock(name=name)
        for name in names
    }
    if "select_python_command" in dependencies:
        dependencies["select_python_command"].return_value = "/custom/python"
    return dependencies


def _assert_dependency_only_storage(service, dependencies) -> None:
    assert vars(service).keys() == dependencies.keys()
    for name, value in dependencies.items():
        assert vars(service)[name] is value
    for name in _LEGACY_SERVICE_ATTRIBUTES:
        assert not hasattr(service, name), name


@pytest.fixture
def runtime_service():
    dependencies = _service_dependencies(_RUNTIME_DEPENDENCIES)
    return SimpleNamespace(
        service=builder_services.RuntimeService(**dependencies), dependencies=dependencies,
    )


@pytest.fixture
def builder(tmp_path):
    """Exercise real facade methods without clients, native pools, or subprocesses."""
    instance = object.__new__(AgentBuilder)
    instance.root_dir = str(tmp_path)
    instance._session = BuildSession()
    instance._artifact_publication_lock = RLock()
    instance._logger = Mock(name="logger")
    for name in (
        "analyzer", "planner", "graph_auditor", "node_planner", "node_coder", "node_auditor",
        "node_test_writer", "node_modifier_writer", "main_writer", "main_entry_auditor",
    ):
        setattr(instance, name, Mock(name=name))
    instance._select_python_command = Mock(return_value="/custom/python")
    instance._popen_subprocess = Mock()
    instance._run_subprocess = Mock()
    instance._subprocess_timeout_expired = Mock(return_value=TimeoutError)
    instance._instantiate_graph = Mock()
    for *_, name in _WORKFLOW_CASES:
        setattr(instance, name, Mock(name=name, spec_set=["run", "close"]))
    instance._runtime_service = Mock(spec_set=[
        "rerun_server", "test_main_entrypoint", "stop_managed_server_process",
    ])
    return instance


def _mock_workflow_constructors(monkeypatch):
    constructors, workflows = {}, {}
    for kind, constructor_name, _, _ in _WORKFLOW_CASES:
        workflows[kind] = Mock(name=f"{kind}_workflow", spec_set=["run", "close"])
        constructors[kind] = Mock(name=constructor_name, return_value=workflows[kind])
        monkeypatch.setattr(agent_builder_module, constructor_name, constructors[kind])
    return constructors, workflows


def _builder_workflow_dependencies(builder):
    return {
        "graph": {
            "analyzer": builder.analyzer, "planner": builder.planner,
            "graph_auditor": builder.graph_auditor,
        },
        "node": {
            "node_planner": builder.node_planner, "node_coder": builder.node_coder,
            "node_auditor": builder.node_auditor,
        },
        "artifact": {
            "node_test_writer": builder.node_test_writer, "node_writer": builder.node_modifier_writer,
            "run_subprocess": builder._run_subprocess, "timeout_expired": TimeoutError,
        },
        "main": {
            "main_writer": builder.main_writer, "main_entry_auditor": builder.main_entry_auditor,
        },
    }


def _published_services(builder):
    return {
        name: getattr(builder, name) for name in _SERVICE_ATTRIBUTES if hasattr(builder, name)
    }


def test_node_generation_reuses_existing_workflow_steps() -> None:
    for step in (NodePlanElement, PromptNodeFileCoderBase, NodeAuditor):
        assert issubclass(step, WorkflowStepNode)
    assert NodeGenerationWorkflow.__annotations__["node_coder"] is not None


@pytest.mark.parametrize("request_class, required_inputs", [
    (NodeGenerationRequest, {
        "graph_plan_path", "requirement_md_path", "requirement_text", "node_meta",
        "raw_node", "target_path", "markdown_path",
    }),
    (NodeRuntimeModifierRequest, {"node_file_name", "workflow_graph", "working_directory"}),
])
def test_merged_node_requests_have_flat_keyword_only_inputs(request_class, required_inputs) -> None:
    parameters = signature(request_class).parameters
    request_fields = fields(request_class)
    field_names = {field.name for field in request_fields}
    removed_fields = {"context", "workflow_name"}
    if request_class is NodeGenerationRequest:
        removed_fields.update(_REMOVED_NODE_GENERATION_FIELDS)
    else:
        removed_fields.update({"completed_stages", "skipped_stages"})
    assert "context" not in parameters
    assert removed_fields.isdisjoint(parameters)
    assert removed_fields.isdisjoint(field_names)
    assert all(parameter.kind is Parameter.KEYWORD_ONLY for parameter in parameters.values())
    assert {name for name, parameter in parameters.items() if parameter.default is Parameter.empty} == required_inputs
    assert all(field.kw_only for field in request_fields if field.init)
    if request_class is NodeGenerationRequest:
        defaults = {
            "language": "python", "temperature": 0.35, "generate_markdown": True,
            "markdown_amendment": "", "node_amendment": "", "dependency_context": "",
            "skip_node_planner": False,
        }
        assert field_names == required_inputs | set(defaults) | {"node_format", "state"}
        assert request_class.__dataclass_params__.frozen
        for name, default in defaults.items():
            assert parameters[name].default == default
        for field in request_fields:
            if field.name in required_inputs:
                assert field.default is field.default_factory is MISSING
            elif field.name in defaults:
                assert field.default == defaults[field.name]
                assert field.default_factory is MISSING
            elif field.name in {"node_format", "state"}:
                assert field.default is MISSING
                assert field.default_factory is dict
        assert all(field.init for field in request_fields)
    else:
        assert parameters["node_name"].default == ""
        outputs = {
            "test_file_path", "log_file_path", "test_log", "test_result",
            "amended_node_file_path",
        }
        assert {field.name for field in request_fields if not field.init} == outputs
        assert outputs.isdisjoint(parameters)


def test_node_generation_request_requires_one_node_and_has_independent_dict_defaults(tmp_path) -> None:
    node_meta = NodeMeta(name="Shared", type="WorkflowStepNode", desc="One step")
    inputs = {
        "graph_plan_path": str(tmp_path / "workflow.json"),
        "requirement_md_path": str(tmp_path / "requirement.md"),
        "requirement_text": "Build one node.", "node_meta": node_meta,
        "raw_node": node_meta.to_dict(), "target_path": tmp_path / "Shared.py",
        "markdown_path": tmp_path / "node_docs" / "Shared.md",
    }
    first, second = NodeGenerationRequest(**inputs), NodeGenerationRequest(**inputs)
    for request in (first, second):
        _assert_complete_node_request(request)
        assert request.skip_node_planner is False
        for name, value in inputs.items():
            assert getattr(request, name) is value
        for field in fields(request):
            with pytest.raises(FrozenInstanceError):
                setattr(request, field.name, getattr(request, field.name))
    assert first.node_format == second.node_format == {}
    assert first.state == second.state == {}
    assert first.node_format is not second.node_format
    assert first.state is not second.state
    first.node_format["user_input_format"] = {"value": "string"}
    first.state["caller_value"] = object()
    assert second.node_format == second.state == {}
    with pytest.raises(TypeError, match="positional"):
        NodeGenerationRequest(*inputs.values())
    for missing in inputs:
        with pytest.raises(TypeError, match="required keyword-only"):
            NodeGenerationRequest(**{name: value for name, value in inputs.items() if name != missing})
    for removed in _REMOVED_NODE_GENERATION_FIELDS:
        with pytest.raises(TypeError, match="unexpected keyword"):
            NodeGenerationRequest(**inputs, **{removed: object()})


def test_node_generation_api_removes_batch_helpers_but_preserves_public_signature(builder) -> None:
    for name in (
        "_generate_nodes", "_normalize_requested_names", "_node_path_for",
        "_render_dependency_context", "_read_node_artifact", "_supports_backend_node_tests",
        "_read_requirement_text", "_start_progress", "_advance_progress", "_print_progress_bar",
        "_generate_node_request", "_run_requested_workflows",
    ):
        assert not hasattr(AgentBuilder, name), name
        assert not hasattr(builder, name), name
    assert not hasattr(NodeGenerationWorkflow, "_run_requested_workflows")
    parameters = signature(AgentBuilder.generate_nodes).parameters
    assert tuple(parameters) == (
        "self", "graph_plan_path", "requirement_md_path", "language", "temperature",
        "node_names", "run_nodes", "generate_markdowns", "markdown_amendments",
        "node_amendments", "workflow_name",
    )
    assert all(parameter.kind is Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters.values())
    assert parameters["self"].default is Parameter.empty
    assert {name: parameter.default for name, parameter in parameters.items() if name != "self"} == {
        "graph_plan_path": None, "requirement_md_path": None, "language": "python",
        "temperature": 0.35, "node_names": None, "run_nodes": None,
        "generate_markdowns": False, "markdown_amendments": None, "node_amendments": None,
        "workflow_name": "default",
    }
    for name in ("callback", "workflow_requests", "workflow_registry"):
        with pytest.raises(TypeError, match="unexpected keyword"):
            builder.generate_nodes(**{name: object()})
    builder._node_workflow.run.assert_not_called()


def test_agent_builder_initialization_has_no_batch_progress_fields(monkeypatch, tmp_path) -> None:
    reset_components, init_services = Mock(), Mock()
    monkeypatch.setattr(AgentBuilder, "_reset_llm_components", reset_components)
    monkeypatch.setattr(AgentBuilder, "_init_services", init_services)
    monkeypatch.setattr(agent_builder_module, "configure_runtime_logging", Mock(return_value=tmp_path / "debug.log"))
    monkeypatch.setattr(agent_builder_module, "get_logger", Mock(return_value=Mock()))
    for name in ("GraphJsonAuditor", "NodeAuditor", "MainEntryPointAuditor"):
        monkeypatch.setattr(agent_builder_module, name, Mock())

    instance = AgentBuilder(api_key="unused", root_dir=str(tmp_path))

    for name in ("_progress_total", "_progress_current", "_progress_width"):
        assert not hasattr(AgentBuilder, name), name
        assert not hasattr(instance, name), name
    reset_components.assert_called_once_with()
    init_services.assert_called_once_with()


@pytest.mark.parametrize("populated", (False, True), ids=("fresh-session", "published-session"))
@pytest.mark.parametrize("arguments", (
    "defaults", "valid", "missing-paths", "empty-graph", "empty-selection",
    "disabled-selection", "empty-arguments", "invalid-workflow",
))
def test_public_node_generation_immediately_rejects_without_side_effects(
    builder, monkeypatch, tmp_path, capsys, populated, arguments,
) -> None:
    graph_path = tmp_path / "requested.json"
    graph_path.write_text(json.dumps({"nodes": [{
        "name": "Shared", "type": "WorkflowStepNode", "desc": "Requested step", "depends": [],
    }]}), encoding="utf-8")
    requirement_path = tmp_path / "requested.md"
    requirement_path.write_text("Build the requested graph.", encoding="utf-8")
    empty_graph_path = tmp_path / "empty.json"
    empty_graph_path.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    node_path, doc_path, test_path = (
        tmp_path / "Shared.py", tmp_path / "Shared.md", tmp_path / "test_Shared.py",
    )
    for path in (node_path, doc_path, test_path):
        path.write_text("# existing artifact\n", encoding="utf-8")
    record = WorkflowArtifactState(
        graph_plan_path="published.json", node_location_map={"Shared": str(node_path)},
        node_doc_paths={"Shared": str(doc_path)},
    )
    if populated:
        builder.graph_plan_path = record.graph_plan_path
        builder.requirement_md_path = "published.md"
        builder.planned_graph = SimpleNamespace(snapshot="published graph")
        builder.workflow_json_path = "published-workflow.json"
        builder.main_output_path = "published-main.py"
        builder.node_location_map = dict(record.node_location_map)
        builder.artifact_state.workflow_artifacts["named"] = record

    options = {
        "graph_plan_path": str(graph_path), "requirement_md_path": str(requirement_path),
        "language": "python", "temperature": 0.45, "node_names": ["Shared"],
        "run_nodes": {"Shared": True}, "generate_markdowns": True,
        "markdown_amendments": {"Shared": "Explain validation"},
        "node_amendments": {"Shared": "Add validation"}, "workflow_name": "named",
    }
    if arguments == "defaults":
        options = {}
    elif arguments == "missing-paths":
        options.update(graph_plan_path=str(tmp_path / "missing.json"), requirement_md_path=str(tmp_path / "missing.md"))
    elif arguments == "empty-graph":
        options.update(graph_plan_path=str(empty_graph_path), node_names=None, run_nodes=None)
    elif arguments == "empty-selection":
        options.update(node_names=[], run_nodes=[])
    elif arguments == "disabled-selection":
        options["run_nodes"] = {"Shared": False}
    elif arguments == "empty-arguments":
        options.update(
            graph_plan_path="", requirement_md_path="", language="", node_names=[],
            run_nodes={}, markdown_amendments={}, node_amendments={}, workflow_name="",
        )
    elif arguments == "invalid-workflow":
        options["workflow_name"] = "   "
    for name in ("_load_planned_graph", "_request_state", "_resolve_root_path"):
        monkeypatch.setattr(builder, name, Mock(name=name))
    storage_snapshots = [
        (owner, dict(vars(owner)))
        for owner in (builder, builder._session, builder.artifact_state, record)
    ]
    artifact_snapshot = deepcopy(builder.artifact_state)
    options_snapshot = deepcopy(options)
    files_before = {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }

    with pytest.raises(NotImplementedError) as raised:
        builder.generate_nodes(**options)

    assert raised.value.args == (
        "Batch node generation is not implemented; use build_nodes() to generate a workflow's nodes.",
    )
    for owner, snapshot in storage_snapshots:
        assert vars(owner).keys() == snapshot.keys()
        for name, value in snapshot.items():
            assert vars(owner)[name] is value, name
    assert builder.artifact_state == artifact_snapshot
    assert options == options_snapshot
    assert {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    } == files_before
    for value in vars(builder).values():
        if isinstance(value, Mock):
            assert value.mock_calls == []
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_runtime_service_exposes_only_explicit_keyword_dependencies() -> None:
    service_fields = fields(builder_services.RuntimeService)
    dependencies = [dependency for dependency in service_fields if dependency.init]
    assert tuple(dependency.name for dependency in dependencies) == _RUNTIME_DEPENDENCIES
    assert all(dependency.kw_only for dependency in dependencies)
    assert not [dependency for dependency in service_fields if not dependency.init]
    injected = _service_dependencies(_RUNTIME_DEPENDENCIES)
    with pytest.raises(TypeError, match="positional"):
        builder_services.RuntimeService(*injected.values())
    for name in (
        "builder", "session", "build_session", "generation_contexts", "workflows", "contexts",
        "modifiers", "_cache_lock", "workflow",
    ):
        with pytest.raises(TypeError, match="unexpected keyword"):
            builder_services.RuntimeService(**injected, **{name: object()})


@pytest.mark.parametrize("method_name, argument_names", [
    ("rerun_server", ("main_entrypoint_path", "previous_process")),
    ("test_main_entrypoint", ("main_entrypoint_path", "log_path")),
])
def test_runtime_execution_methods_require_explicit_keyword_arguments(
    runtime_service, method_name, argument_names,
) -> None:
    parameters = signature(getattr(builder_services.RuntimeService, method_name)).parameters
    assert tuple(parameters) == ("self", *argument_names)
    assert all(parameters[name].kind is Parameter.KEYWORD_ONLY for name in argument_names)
    if method_name == "rerun_server":
        assert parameters["previous_process"].default is None
    with pytest.raises(TypeError, match="positional"):
        getattr(runtime_service.service, method_name)(*("unused" for _ in argument_names))
    runtime_service.dependencies["popen_subprocess"].assert_not_called()
    runtime_service.dependencies["run_subprocess"].assert_not_called()
    _assert_dependency_only_storage(runtime_service.service, runtime_service.dependencies)


def test_runtime_service_returns_explicit_process_data_without_retaining_it(
    runtime_service, monkeypatch, tmp_path,
) -> None:
    service, dependencies = runtime_service.service, runtime_service.dependencies
    environment = {"FLOWX_RUNTIME_TEST": "offline"}
    monkeypatch.setattr(builder_services.os, "environ", environment)
    previous = Mock(spec_set=["poll", "terminate", "wait", "kill"])
    previous.poll.return_value = None
    first = Mock(spec_set=["pid", "poll", "terminate", "wait", "kill"], pid=123)
    second = SimpleNamespace(pid=456)
    dependencies["popen_subprocess"].side_effect = [first, second]
    first_path = str(tmp_path / "unused" / ".." / "main.py")
    second_path = str(tmp_path / "another" / "server.py")

    process, backend = service.rerun_server(main_entrypoint_path=first_path, previous_process=previous)
    assert process is first
    assert backend == {
        "pid": 123, "command": ["/custom/python", str(tmp_path / "main.py")], "cwd": str(tmp_path),
    }
    process, backend = service.rerun_server(main_entrypoint_path=second_path)
    assert process is second
    assert backend == {
        "pid": 456, "command": ["/custom/python", second_path], "cwd": str(tmp_path / "another"),
    }

    previous.poll.assert_called_once_with()
    previous.terminate.assert_called_once_with()
    previous.wait.assert_called_once_with(timeout=5)
    previous.kill.assert_not_called()
    first.poll.assert_not_called()
    first.terminate.assert_not_called()
    assert dependencies["popen_subprocess"].call_args_list == [
        call(["/custom/python", str(tmp_path / "main.py")], cwd=str(tmp_path), env=environment),
        call(["/custom/python", second_path], cwd=str(tmp_path / "another"), env=environment),
    ]
    assert all(
        actual.kwargs["env"] is not environment
        for actual in dependencies["popen_subprocess"].call_args_list
    )
    assert dependencies["select_python_command"].call_args_list == [call(), call()]
    dependencies["run_subprocess"].assert_not_called()
    _assert_dependency_only_storage(service, dependencies)


@pytest.mark.parametrize("outcome", ("success", "failure", "timeout", "exception"))
def test_runtime_service_writes_requested_log_without_auditing_or_publishing_state(
    runtime_service, tmp_path, outcome,
) -> None:
    service, dependencies = runtime_service.service, runtime_service.dependencies
    main_path = str(tmp_path / "main.py")
    log_path = tmp_path / "logs" / "main_test.txt"
    if outcome == "timeout":
        dependencies["run_subprocess"].side_effect = TimeoutError("execution timed out")
        expected = "Test timed out after 60 seconds."
    elif outcome == "exception":
        dependencies["run_subprocess"].side_effect = RuntimeError("execution failed")
        expected = "Test raised an exception: RuntimeError: execution failed"
    else:
        returncode = 0 if outcome == "success" else 7
        dependencies["run_subprocess"].return_value = SimpleNamespace(
            returncode=returncode, stdout="example output", stderr="example diagnostic",
        )
        expected = "Test completed successfully." if outcome == "success" else "Test failed with return code 7."

    assert service.test_main_entrypoint(main_entrypoint_path=main_path, log_path=str(log_path)) is None

    text = log_path.read_text(encoding="utf-8")
    assert "=== Main Entrypoint Test Log ===" in text
    assert f"Testing file: {main_path}" in text
    assert expected in text
    assert "Test ended:" in text
    if outcome in ("success", "failure"):
        assert "--- STDOUT ---\nexample output" in text
        assert "--- STDERR ---\nexample diagnostic" in text
    dependencies["run_subprocess"].assert_called_once_with(
        ["/custom/python", main_path], cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
    )
    dependencies["select_python_command"].assert_called_once_with()
    dependencies["popen_subprocess"].assert_not_called()
    dependencies["logger"].info.assert_called_once_with("Test log written to: %s", str(log_path))
    _assert_dependency_only_storage(service, dependencies)


@pytest.mark.parametrize("failed_kind", (None, "graph", "node", "artifact", "main"))
def test_agent_builder_close_closes_all_four_workflows_even_if_cleanup_fails(failed_kind) -> None:
    builder = SimpleNamespace(**{
        name: Mock(name=name, spec_set=["close"]) for *_, name in _WORKFLOW_CASES
    })
    closes = Mock()
    failure = RuntimeError("workflow cleanup failed")
    for kind, *_, name in _WORKFLOW_CASES:
        workflow = getattr(builder, name)
        closes.attach_mock(workflow.close, kind)
        if kind == failed_kind:
            workflow.close.side_effect = failure
    if failed_kind is None:
        assert AgentBuilder.close(builder) is None
    else:
        with pytest.raises(RuntimeError) as raised:
            AgentBuilder.close(builder)
        assert raised.value is failure
    assert closes.mock_calls == [getattr(call, case[0])() for case in reversed(_WORKFLOW_CASES)]
    for *_, name in _WORKFLOW_CASES:
        getattr(builder, name).close.assert_called_once_with()


def test_agent_builder_initializes_workflows_before_publishing_a_complete_service_set(
    builder, monkeypatch,
) -> None:
    for name in _SERVICE_ATTRIBUTES:
        delattr(builder, name)
    constructors, workflows = _mock_workflow_constructors(monkeypatch)
    expected = _builder_workflow_dependencies(builder)

    def construct(workflow):
        def create(**kwargs):
            assert _published_services(builder) == {}
            return workflow
        return create

    for kind, constructor in constructors.items():
        constructor.side_effect = construct(workflows[kind])
    runtime = object()

    def construct_runtime(**kwargs):
        assert set(_published_services(builder)) == set(_SERVICE_ATTRIBUTES[:-1])
        for kind, _, _, name in _WORKFLOW_CASES:
            assert getattr(builder, name) is workflows[kind]
        return runtime

    runtime_constructor = Mock(side_effect=construct_runtime)
    monkeypatch.setattr(agent_builder_module, "RuntimeService", runtime_constructor)

    builder._init_services()

    for kind, _, _, name in _WORKFLOW_CASES:
        constructors[kind].assert_called_once_with(**expected[kind])
        assert getattr(builder, name) is workflows[kind]
        workflows[kind].run.assert_not_called()
        workflows[kind].close.assert_not_called()
    runtime_constructor.assert_called_once_with(
        logger=builder._logger, select_python_command=builder._select_python_command,
        popen_subprocess=builder._popen_subprocess, run_subprocess=builder._run_subprocess,
        timeout_expired=TimeoutError,
    )
    assert builder._runtime_service is runtime


@pytest.mark.parametrize("failed_index", range(len(_WORKFLOW_CASES)), ids=("graph", "node", "artifact", "main"))
@pytest.mark.parametrize("preserve_existing", (False, True), ids=("unpublished", "existing-services"))
def test_agent_builder_initialization_failure_closes_earlier_workflows_without_partial_publication(
    builder, monkeypatch, failed_index, preserve_existing,
) -> None:
    if not preserve_existing:
        for name in _SERVICE_ATTRIBUTES:
            delattr(builder, name)
    previous = _published_services(builder)
    constructors, workflows = _mock_workflow_constructors(monkeypatch)
    expected = _builder_workflow_dependencies(builder)
    failed_kind = _WORKFLOW_CASES[failed_index][0]
    failure = RuntimeError(f"{failed_kind} construction failed")
    constructors[failed_kind].side_effect = failure
    runtime_constructor = Mock()
    monkeypatch.setattr(agent_builder_module, "RuntimeService", runtime_constructor)
    closes = Mock()
    for kind, workflow in workflows.items():
        closes.attach_mock(workflow.close, kind)

    with pytest.raises(RuntimeError) as raised:
        builder._init_services()

    assert raised.value is failure
    assert _published_services(builder) == previous
    for index, (kind, _, _, name) in enumerate(_WORKFLOW_CASES):
        if index <= failed_index:
            constructors[kind].assert_called_once_with(**expected[kind])
        else:
            constructors[kind].assert_not_called()
        if index < failed_index:
            workflows[kind].close.assert_called_once_with()
        else:
            workflows[kind].close.assert_not_called()
        workflows[kind].run.assert_not_called()
        if preserve_existing:
            previous[name].close.assert_not_called()
    assert closes.mock_calls == [
        getattr(call, case[0])() for case in reversed(_WORKFLOW_CASES[:failed_index])
    ]
    runtime_constructor.assert_not_called()


def test_agent_builder_initialization_still_cleans_up_if_an_earlier_workflow_close_raises(
    builder, monkeypatch,
) -> None:
    previous = _published_services(builder)
    constructors, workflows = _mock_workflow_constructors(monkeypatch)
    constructors["main"].side_effect = RuntimeError("main construction failed")
    cleanup_failure = RuntimeError("artifact cleanup failed")
    workflows["artifact"].close.side_effect = cleanup_failure

    with pytest.raises(RuntimeError) as raised:
        builder._init_services()

    assert raised.value is cleanup_failure
    assert _published_services(builder) == previous
    for kind in ("graph", "node", "artifact"):
        workflows[kind].close.assert_called_once_with()
    workflows["main"].close.assert_not_called()


def test_agent_builder_request_state_is_fresh_and_references_its_own_session(builder) -> None:
    first, second = builder._request_state(), builder._request_state()
    assert first == second == {"build_session": builder._session}
    assert first is not second
    assert first["build_session"] is second["build_session"] is builder._session
    first["caller_value"] = object()
    assert "caller_value" not in second


def _generation_files(builder, tmp_path):
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{
        "name": "Shared", "type": "WorkflowStepNode", "meta_node_kind": "WorkflowStepNode",
        "desc": "Original step", "depends": [], "ext_data": {"type": "none"},
    }]}), encoding="utf-8")
    requirement = tmp_path / "requirement.md"
    requirement.write_text("Build the current graph.", encoding="utf-8")
    builder.graph_plan_path = str(graph_path)
    builder.requirement_md_path = str(requirement)
    return graph_path


@pytest.mark.parametrize("explicit_path", (False, True), ids=("session-path", "explicit-path"))
def test_load_planned_graph_refreshes_same_path_without_mutating_existing_snapshots(
    builder, tmp_path, explicit_path,
) -> None:
    graph_path = _generation_files(builder, tmp_path)
    original_graph = builder._load_planned_graph()
    original_nodes = deepcopy(original_graph.nodes)
    original_meta = original_graph.get_node_meta("Shared")
    original_meta_values = deepcopy(original_meta.to_dict())
    replacement = {"nodes": [{
        "name": "Shared", "type": "WorkflowFileNode", "meta_node_kind": "WorkflowFileNode",
        "desc": "Replacement upload", "depends": [],
        "ext_data": {"type": "user_file_input", "remote_desc": "Store uploads"},
    }, {
        "name": "Added", "type": "WorkflowSkillNode", "meta_node_kind": "WorkflowSkillNode",
        "desc": "New skill", "depends": ["Shared"], "inputs_format": {"summary": "string"},
        "ext_data": {"type": "skill", "skill_name": "summarize"},
    }]}
    graph_path.write_text(json.dumps(replacement), encoding="utf-8")
    selected_path = str(graph_path) if explicit_path else None
    assert builder._load_planned_graph(selected_path) is original_graph

    refreshed = builder._load_planned_graph(selected_path, refresh=True)

    assert refreshed is builder.planned_graph
    assert refreshed is not original_graph
    assert refreshed.graph_json_path == graph_path
    assert builder.graph_plan_path == str(graph_path)
    assert refreshed.get_topological_sorted_nodes() == ["Shared", "Added"]
    assert refreshed.nodes == replacement["nodes"]
    for raw_node in replacement["nodes"]:
        meta = refreshed.get_node_meta(raw_node["name"])
        assert meta is not original_meta
        assert meta.meta_node_kind == raw_node["meta_node_kind"]
        assert meta.desc == raw_node["desc"]
        assert meta.depends == raw_node["depends"]
    assert refreshed.get_node_meta("Added").inputs_format == {"summary": "string"}
    assert builder._load_planned_graph(selected_path) is refreshed
    assert original_graph.get_topological_sorted_nodes() == ["Shared"]
    assert original_graph.nodes == original_nodes
    assert original_graph.get_node_meta("Shared") is original_meta
    assert original_meta.to_dict() == original_meta_values
    assert original_graph.get_node_meta("Added") is None
    assert builder.artifact_state.workflow_artifacts == {}
    assert builder.node_location_map == {}
    builder._node_workflow.run.assert_not_called()
    builder._modifier_pipeline.run.assert_not_called()


@pytest.mark.parametrize("explicit_path", (False, True), ids=("session-path", "explicit-path"))
def test_failed_planned_graph_refresh_does_not_retain_old_topology(builder, tmp_path, explicit_path) -> None:
    graph_path = _generation_files(builder, tmp_path)
    old_graph = builder._load_planned_graph()
    old_nodes = deepcopy(old_graph.nodes)
    graph_path.write_text("not JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        builder._load_planned_graph(str(graph_path) if explicit_path else None, refresh=True)
    assert builder.planned_graph is None
    assert builder.graph_plan_path == str(graph_path)
    assert old_graph.get_topological_sorted_nodes() == ["Shared"]
    assert old_graph.nodes == old_nodes
    assert old_graph.get_node_meta("Shared").desc == "Original step"
    assert builder.artifact_state.workflow_artifacts == {}
    builder._node_workflow.run.assert_not_called()
    builder._modifier_pipeline.run.assert_not_called()


def test_agent_builder_graph_facade_delegates_and_publishes_shared_artifacts(builder, tmp_path) -> None:
    planned_path, amended_path = str(tmp_path / "planned.json"), str(tmp_path / "amended.json")
    existing_requirement = str(tmp_path / "existing.md")
    planned_graph, amended_graph = object(), object()
    builder._instantiate_graph.side_effect = [planned_graph, amended_graph]
    builder._graph_workflow.run.side_effect = [
        SimpleNamespace(outputs={
            "analyze_requirement": StepRunOutput(derived={"requirement_md_path": "analyzed.md"}),
        }),
        SimpleNamespace(outputs={
            "plan_graph": StepRunOutput(derived={"graph_plan_json_path": "unused-plan.json"}),
            "audit_graph": StepRunOutput(derived={
                "graph_plan_json_path": planned_path, "ok": True, "violations": [],
            }),
        }),
        SimpleNamespace(outputs={
            "plan_graph": StepRunOutput(derived={"graph_plan_json_path": "unused-amendment.json"}),
            "audit_graph": StepRunOutput(derived={
                "graph_plan_json_path": amended_path, "ok": True, "violations": [],
            }),
        }),
        SimpleNamespace(outputs={}),
    ]

    assert builder.analyze_requirement("Build a workflow") == "analyzed.md"
    assert builder._session.requirement_md_path == "analyzed.md"
    builder._instantiate_graph.assert_not_called()
    assert builder.plan_graph(temperature=0.45) == planned_path
    assert builder._session.graph_plan_path == planned_path
    assert builder._session.planned_graph is planned_graph
    assert builder.amend_workflow_json("Add validation", temperature=0.55) == amended_path
    assert builder._session.graph_plan_path == amended_path
    assert builder._session.planned_graph is amended_graph
    assert builder.analyze_requirement(requirement_file=existing_requirement) == existing_requirement
    assert builder._session.requirement_md_path == existing_requirement
    assert builder._session.graph_plan_path == amended_path
    assert builder._session.planned_graph is amended_graph
    assert builder._instantiate_graph.call_args_list == [call(planned_path), call(amended_path)]
    state = {"build_session": builder._session}
    assert builder._graph_workflow.run.call_args_list == [
        call(GraphGenerationRequest(
            requirement_text="Build a workflow",
            requirement_md_path=str(tmp_path / "requirement_analysis.md"),
            skip_plan_graph=True, skip_audit_graph=True, state=state,
        )),
        call(GraphGenerationRequest(
            requirement_md_path="analyzed.md", graph_plan_path=str(tmp_path / "workflow.json"),
            temperature=0.45, skip_analyze_requirement=True, state=state,
        )),
        call(GraphGenerationRequest(
            graph_plan_path=planned_path, graph_action="amend", amendment="Add validation",
            temperature=0.55, skip_analyze_requirement=True, state=state,
        )),
        call(GraphGenerationRequest(
            requirement_md_path=existing_requirement,
            skip_analyze_requirement=True, skip_plan_graph=True, skip_audit_graph=True, state=state,
        )),
    ]
    for actual in builder._graph_workflow.run.call_args_list:
        assert actual.args[0].state["build_session"] is builder._session
    builder.analyzer.assert_not_called()
    builder.planner.assert_not_called()
    builder.graph_auditor.assert_not_called()


@pytest.mark.parametrize("skip_analysis, skip_plan, skip_audit", product((False, True), repeat=3))
def test_agent_builder_graph_request_merges_state_and_publishes_selected_normalized_outputs(
    builder, tmp_path, skip_analysis, skip_plan, skip_audit,
) -> None:
    builder.requirement_md_path = "old.md"
    builder.graph_plan_path = "old.json"
    builder.planned_graph = object()
    caller_session, callback, marker = BuildSession(), Mock(), object()
    state = MappingProxyType({"build_session": caller_session, "callback": callback, "marker": marker})
    requested_requirement, requested_graph = "requested.txt", "requested.plan"
    analyzed, planned, audited = "analyzed.md", "planned.json", "audited.graph.json"
    graph = object()
    # Include conflicting skipped-step outputs to ensure they are not published.
    result = SimpleNamespace(outputs={
        "analyze_requirement": StepRunOutput(derived={"requirement_md_path": analyzed}),
        "plan_graph": StepRunOutput(derived={"graph_plan_json_path": planned}),
        "audit_graph": StepRunOutput(derived={"graph_plan_json_path": audited, "ok": True, "violations": []}),
    })
    request = GraphGenerationRequest(
        requirement_text="Build a workflow", requirement_md_path=requested_requirement,
        graph_plan_path=requested_graph, requirement_temperature=0.15, temperature=0.6,
        skip_analyze_requirement=skip_analysis, skip_plan_graph=skip_plan, skip_audit_graph=skip_audit,
        state=state,
    )

    def run(forwarded):
        assert builder.requirement_md_path == requested_requirement
        assert builder.graph_plan_path == requested_graph
        assert builder.planned_graph is None
        assert forwarded is not request
        for field in fields(request):
            if field.name != "state":
                assert getattr(forwarded, field.name) is getattr(request, field.name)
        assert forwarded.state is not state
        assert forwarded.state["build_session"] is builder._session
        assert forwarded.state["callback"] is callback
        assert forwarded.state["marker"] is marker
        return result

    builder._graph_workflow.run.side_effect = run
    expected_graph = requested_graph if skip_plan and skip_audit else (planned if skip_audit else audited)

    def instantiate(path):
        assert path == builder.graph_plan_path == expected_graph
        assert builder.planned_graph is None
        return graph

    builder._instantiate_graph.side_effect = instantiate

    assert builder._run_graph_request(request) is result

    builder._graph_workflow.run.assert_called_once()
    assert builder.requirement_md_path == (requested_requirement if skip_analysis else analyzed)
    assert builder.graph_plan_path == expected_graph
    if skip_plan and skip_audit:
        assert builder.planned_graph is None
        builder._instantiate_graph.assert_not_called()
    else:
        assert builder.planned_graph is graph
        builder._instantiate_graph.assert_called_once_with(expected_graph)
    assert request.state is state
    assert state["build_session"] is caller_session
    callback.assert_not_called()


@pytest.mark.parametrize("action, skip_plan, skip_audit, path_choice, invalidates", [
    pytest.param("plan", False, False, "same", True, id="same-path-plan"),
    pytest.param("amend", False, False, "same", True, id="same-path-amendment"),
    pytest.param("plan", True, False, "different", True, id="changed-path-audit"),
    pytest.param("plan", True, True, "different", True, id="changed-path-all-skipped"),
    pytest.param("plan", True, False, "same", False, id="same-path-audit-only"),
    pytest.param("plan", True, True, "same", False, id="same-path-all-skipped"),
    pytest.param("plan", True, True, None, False, id="no-path-all-skipped"),
])
def test_agent_builder_graph_failure_does_not_expose_stale_written_or_replaced_topology(
    builder, action, skip_plan, skip_audit, path_choice, invalidates,
) -> None:
    old_graph = object()
    builder.graph_plan_path = "existing.json"
    builder.planned_graph = old_graph
    path = "existing.json" if path_choice == "same" else ("replacement.json" if path_choice else None)
    request = GraphGenerationRequest(
        graph_plan_path=path, graph_action=action, amendment="Update graph",
        skip_analyze_requirement=True, skip_plan_graph=skip_plan, skip_audit_graph=skip_audit,
    )
    failure = RuntimeError("graph request failed")

    def fail(forwarded):
        assert builder.planned_graph is (None if invalidates else old_graph)
        assert builder.graph_plan_path == (path or "existing.json")
        assert forwarded.state["build_session"] is builder._session
        raise failure

    builder._graph_workflow.run.side_effect = fail
    with pytest.raises(RuntimeError) as raised:
        builder._run_graph_request(request)
    assert raised.value is failure
    assert builder.planned_graph is (None if invalidates else old_graph)
    builder._graph_workflow.run.assert_called_once()
    builder._instantiate_graph.assert_not_called()


def test_agent_builder_graph_instantiation_failure_cannot_restore_the_old_topology(builder) -> None:
    builder.graph_plan_path = "existing.json"
    builder.planned_graph = object()
    builder._graph_workflow.run.return_value = SimpleNamespace(outputs={
        "audit_graph": StepRunOutput(derived={"graph_plan_json_path": "normalized.json"}),
    })
    failure = RuntimeError("graph cannot be loaded")
    builder._instantiate_graph.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        builder._run_graph_request(GraphGenerationRequest(
            graph_plan_path="existing.json", skip_analyze_requirement=True,
        ))
    assert raised.value is failure
    assert builder.graph_plan_path == "normalized.json"
    assert builder.planned_graph is None
    builder._instantiate_graph.assert_called_once_with("normalized.json")


def test_agent_builder_main_facade_seeds_session_and_publishes_normalized_output(builder, tmp_path) -> None:
    graph_path = str(tmp_path / "workflow.json")
    normalized_path = str(tmp_path / "normalized_main.py")
    builder._main_workflow.run.return_value = normalized_path

    assert builder.generate_main_entrypoint(
        graph_path, output_filename="entrypoints/main.py", fastapi_host="127.0.0.1",
        temperature=0.4, fastapi_port=9000,
    ) == normalized_path

    builder._main_workflow.run.assert_called_once_with(MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=graph_path,
        output_path=str(tmp_path / "entrypoints" / "main.py"), fastapi_host="127.0.0.1",
        fastapi_port=9000, state={"build_session": builder._session},
    ))
    forwarded = builder._main_workflow.run.call_args.args[0]
    assert "temperature" in signature(AgentBuilder.generate_main_entrypoint).parameters
    assert "temperature" not in signature(MainEntrypointGenerationRequest).parameters
    assert "temperature" not in {field.name for field in fields(MainEntrypointGenerationRequest)}
    assert not hasattr(forwarded, "temperature")
    assert forwarded.state["build_session"] is builder._session
    assert builder._session.main_output_path == normalized_path


def test_main_entrypoint_workflow_sends_template_payload_without_temperature(monkeypatch, tmp_path) -> None:
    # Bypass eager pool construction and record the exact native call instead.
    workflow = object.__new__(main_entrypoint_module.MainEntrypointGenerationWorkflow)
    workflow.main_writer = SimpleNamespace(STEP_ID="main_generate")
    workflow.main_entry_auditor = SimpleNamespace(STEP_ID="main_audit")
    workflow._engine = object()
    state = MappingProxyType({"marker": object()})
    output_path, normalized_path = str(tmp_path / "main.py"), str(tmp_path / "normalized_main.py")
    request = MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=str(tmp_path / "workflow.json"),
        output_path=output_path, fastapi_host="127.0.0.1", fastapi_port=9000, state=state,
    )
    native_run = Mock(return_value=SimpleNamespace(outputs={
        "main_generate": StepRunOutput(derived={"main_entrypoint_path": normalized_path}),
        "main_audit": StepRunOutput(derived={"ok": True, "violations": []}),
    }))
    monkeypatch.setattr(main_entrypoint_module, "run_workflow", native_run)

    assert workflow.run(request) == normalized_path

    native_run.assert_called_once_with(workflow._engine, state={
        **state,
        f"{type(workflow.main_writer).__name__}::action": "write",
        "main_entrypoint": {
            "project_root_path": str(tmp_path), "graph_plan_json_path": str(tmp_path / "workflow.json"),
            "output_path": output_path, "fastapi_host": "127.0.0.1", "fastapi_port": 9000,
        },
        "auditor": {"file_path": output_path, "nodes_root": str(tmp_path)},
    })
    payload = native_run.call_args.kwargs["state"]
    assert "temperature" not in payload["main_entrypoint"]
    assert not hasattr(request, "temperature")
    assert request.state is state
    assert payload is not state
    assert payload["marker"] is state["marker"]
    assert set(state) == {"marker"}