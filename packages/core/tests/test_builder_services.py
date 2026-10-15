"""Modified 2026-10-14: construct services from explicit dependencies without a builder.

Modified 2026-10-14: verify graph-workflow request delegation and shared artifact state.
Modified 2026-10-14: test eager workflow ownership and builder-owned request publication.
Modified 2026-10-15: test unified generation requests and caller-isolated mutable repair outputs.
Modified 2026-10-15: verify cleaned request APIs, builder-local routing and deterministic main payloads.
Modified 2026-10-15: migrate generation coverage to complete per-node requests without batch/callback state.
Modified 2026-10-15: replace removed batch coverage with immediate rejection and direct graph refresh tests.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import MISSING, FrozenInstanceError, fields, replace
from inspect import Parameter, signature
from itertools import product
import json
from pathlib import Path
from threading import Barrier, Event, RLock
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


_THREAD_TIMEOUT = 5
_WORKFLOW_SERVICE_CASES = (
    (
        "graph", builder_services.GraphBuildService, "GraphGenerationWorkflow",
        ("analyzer", "planner", "graph_auditor"), "_graph_build_service",
    ),
    (
        "node", builder_services.NodeBuildService, "NodeGenerationWorkflow",
        ("node_planner", "node_coder", "node_auditor"), "_node_build_service",
    ),
    (
        "artifact", builder_services.NodeArtifactService, "NodeRuntimeModifierPipeline",
        ("node_test_writer", "node_writer", "run_subprocess", "timeout_expired"),
        "_node_artifact_service",
    ),
    (
        "main", builder_services.MainEntrypointService, "MainEntrypointGenerationWorkflow",
        ("main_writer", "main_entry_auditor"), "_main_entrypoint_service",
    ),
)
_RUNTIME_DEPENDENCIES = (
    "logger", "select_python_command", "popen_subprocess", "run_subprocess", "timeout_expired",
)
_SERVICE_ATTRIBUTES = tuple(case[4] for case in _WORKFLOW_SERVICE_CASES) + ("_runtime_service",)
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


def _assert_dependency_only_storage(service, dependencies, workflow=None) -> None:
    expected = dict(dependencies)
    if workflow is not None:
        expected["workflow"] = workflow
    assert vars(service).keys() == expected.keys()
    for name, value in expected.items():
        assert vars(service)[name] is value
    for name in _LEGACY_SERVICE_ATTRIBUTES:
        assert not hasattr(service, name), name


def _service_requests(kind, root):
    """Use real requests; per-node inputs, repair outputs and state are caller-owned."""
    requests, results = [], []
    for index, (workflow_name, node_name, node_kind) in enumerate((
        ("default", "Extract", "WorkflowStepNode"),
        ("default", "Upload", "WorkflowFileNode"),
        ("another-workflow", "Summarize", "WorkflowSkillNode"),
    )):
        project_root = root / workflow_name
        graph_path = str(project_root / "workflow.json")
        requirement_path = str(project_root / f"{node_name}.md")
        target_path = project_root / f"{node_name}.py"
        state = MappingProxyType({
            "build_session": BuildSession(), "request_id": f"{kind}-{index}",
            "workflow_name": workflow_name, "node_name": node_name, "meta_node_kind": node_kind,
            "callback": Mock(name=f"{kind}_{node_name}_callback"),
        })
        if kind == "graph":
            item = GraphGenerationRequest(
                requirement_text=f"Build {node_name}", requirement_md_path=requirement_path,
                graph_plan_path=graph_path, graph_action="amend" if index == 2 else "plan",
                amendment="Add validation" if index == 2 else "", temperature=0.3 + index / 10,
                skip_analyze_requirement=index == 2, state=state,
            )
            result = SimpleNamespace(outputs={"opaque": object()})
        elif kind == "node":
            node_meta = NodeMeta(
                name=node_name, type=node_kind, desc=f"Build {node_name}", meta_node_kind=node_kind,
            )
            item = NodeGenerationRequest(
                graph_plan_path=graph_path, requirement_md_path=requirement_path,
                requirement_text=f"Build {node_name}", node_meta=node_meta,
                raw_node=node_meta.to_dict(), target_path=target_path,
                markdown_path=project_root / "node_docs" / f"{node_name}.md",
                language="python", temperature=0.35, generate_markdown=index != 1,
                markdown_amendment=f"Document {node_name}", node_amendment=f"Validate {node_name}",
                skip_node_planner=index == 1,
                node_format={"user_input_format": {"value": "string"}},
                dependency_context=f"Earlier artifact for {node_name}", state=state,
            )
            result = str(target_path)
        elif kind == "artifact":
            item = NodeRuntimeModifierRequest(
                node_file_name=str(target_path), workflow_graph=graph_path,
                working_directory=str(project_root), node_name=node_name,
                skip_generate_node_test=index == 1, skip_run_node_test=index == 2,
                skip_amend_node=True, state=state,
            )
            result = replace(item)
            result.test_file_path = str(project_root / "tests" / f"test_{node_name}.py")
            if not item.skip_run_node_test:
                result.log_file_path = str(project_root / "logs" / f"{node_name}_test.log")
                result.test_log = f"Test output for {node_name}"
                result.test_result = {"ok": True, "returncode": 0}
            result.amended_node_file_path = str(target_path)
        else:
            assert kind == "main"
            item = MainEntrypointGenerationRequest(
                project_root_path=str(project_root), graph_plan_json_path=graph_path,
                output_path=str(project_root / f"{node_name}_main.py"),
                fastapi_port=8000 + index, state=state,
            )
            result = str(project_root / f"audited_{node_name}_main.py")
        requests.append(item)
        results.append(result)
    return tuple(requests), tuple(results)


@pytest.fixture(params=_WORKFLOW_SERVICE_CASES, ids=lambda case: case[0])
def workflow_service(request, monkeypatch, tmp_path):
    kind, service_class, constructor_name, dependency_names, _ = request.param
    dependencies = _service_dependencies(dependency_names)
    workflow = Mock(name=f"{kind}_workflow", spec_set=["run", "close"])
    constructor = Mock(name=constructor_name, return_value=workflow)
    # Patch the imported constructor before __post_init__ can create a native pool.
    monkeypatch.setattr(builder_services, constructor_name, constructor)
    service = service_class(**dependencies)
    requests, results = _service_requests(kind, tmp_path)
    return SimpleNamespace(
        service=service, dependencies=dependencies, workflow=workflow, constructor=constructor,
        requests=requests, results=results,
    )


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
    for _, _, _, _, name in _WORKFLOW_SERVICE_CASES:
        setattr(instance, name, Mock(name=name, spec_set=["run", "close"]))
    instance._runtime_service = Mock(spec_set=[
        "rerun_server", "test_main_entrypoint", "stop_managed_server_process",
    ])
    instance.output_auditor = Mock(spec_set=["audit_log_file"])
    return instance


def _mock_workflow_constructors(monkeypatch):
    constructors, workflows = {}, {}
    for kind, _, constructor_name, _, _ in _WORKFLOW_SERVICE_CASES:
        workflows[kind] = Mock(name=f"{kind}_workflow", spec_set=["run", "close"])
        constructors[kind] = Mock(name=constructor_name, return_value=workflows[kind])
        monkeypatch.setattr(builder_services, constructor_name, constructors[kind])
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
    builder._node_build_service.run.assert_not_called()


def test_agent_builder_initialization_has_no_batch_progress_fields(monkeypatch, tmp_path) -> None:
    reset_components, init_services = Mock(), Mock()
    monkeypatch.setattr(AgentBuilder, "_reset_llm_components", reset_components)
    monkeypatch.setattr(AgentBuilder, "_init_services", init_services)
    monkeypatch.setattr(agent_builder_module, "configure_runtime_logging", Mock(return_value=tmp_path / "debug.log"))
    monkeypatch.setattr(agent_builder_module, "get_logger", Mock(return_value=Mock()))
    for name in ("GraphJsonAuditor", "NodeAuditor", "MainEntryPointAuditor", "OutputAuditor"):
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
        builder.node_doc_paths = [str(doc_path)]
        builder.node_docs_dir = str(tmp_path)
        builder.node_coder_map = {"Shared": SimpleNamespace(kind="existing coder")}
        builder.artifact_state.workflow_artifacts["named"] = record
        builder.dynamic_graph_cache["node_tests"] = {"Shared": str(test_path)}
        builder.dynamic_graph_cache["workflow_node_tests"] = {"named": {"Shared": str(test_path)}}
        builder.dynamic_graph_cache["server_runtime"] = {"backend": {"pid": 123}}
        builder.backend_server_process = SimpleNamespace(pid=123)
        builder.log_path = "published.log"

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
    for name in ("_load_planned_graph", "_request_state", "_resolve_root_path", "run_node_modifier"):
        monkeypatch.setattr(builder, name, Mock(name=name))
    storage_snapshots = [
        (owner, dict(vars(owner)))
        for owner in (builder, builder._session, builder.artifact_state, builder.runtime_state, record)
    ]
    artifact_snapshot, runtime_snapshot = deepcopy(builder.artifact_state), deepcopy(builder.runtime_state)
    options_snapshot = deepcopy(options)
    files_before = {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }

    with pytest.raises(NotImplementedError) as raised:
        builder.generate_nodes(**options)

    assert raised.value.args == (
        "Batch node generation is not implemented; submit a NodeGenerationRequest to NodeBuildService.run() instead.",
    )
    for owner, snapshot in storage_snapshots:
        assert vars(owner).keys() == snapshot.keys()
        for name, value in snapshot.items():
            assert vars(owner)[name] is value, name
    assert builder.artifact_state == artifact_snapshot
    assert builder.runtime_state == runtime_snapshot
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


@pytest.mark.parametrize("service_class, dependency_names, owns_workflow", [
    pytest.param(service_class, names, True, id=kind)
    for kind, service_class, _, names, _ in _WORKFLOW_SERVICE_CASES
] + [pytest.param(builder_services.RuntimeService, _RUNTIME_DEPENDENCIES, False, id="runtime")])
def test_services_expose_only_explicit_keyword_dependencies(
    service_class, dependency_names, owns_workflow,
) -> None:
    service_fields = fields(service_class)
    dependencies = [dependency for dependency in service_fields if dependency.init]
    assert tuple(dependency.name for dependency in dependencies) == dependency_names
    assert all(dependency.kw_only for dependency in dependencies)
    assert {dependency.name for dependency in service_fields if not dependency.init} == (
        {"workflow"} if owns_workflow else set()
    )
    injected = _service_dependencies(dependency_names)
    with pytest.raises(TypeError, match="positional"):
        service_class(*injected.values())
    for name in (
        "builder", "session", "build_session", "generation_contexts", "workflows", "contexts",
        "modifiers", "_cache_lock", "workflow",
    ):
        with pytest.raises(TypeError, match="unexpected keyword"):
            service_class(**injected, **{name: object()})


def test_services_construct_one_workflow_before_any_request(workflow_service) -> None:
    case = workflow_service
    case.constructor.assert_called_once_with(**case.dependencies)
    assert case.service.workflow is case.workflow
    case.workflow.run.assert_not_called()
    case.workflow.close.assert_not_called()
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


def test_services_repeatedly_delegate_different_requests_without_retaining_state(workflow_service) -> None:
    case = workflow_service
    results = {id(request): result for request, result in zip(case.requests, case.results)}
    states = tuple(request.state for request in case.requests)
    snapshots = tuple(
        {field.name: getattr(request, field.name) for field in fields(request)}
        for request in case.requests
    )
    case.workflow.run.side_effect = lambda request: results[id(request)]
    requests = (*case.requests, case.requests[0], case.requests[2])
    for request in requests:
        assert case.service.run(request) is results[id(request)]
    assert case.workflow.run.call_args_list == [call(request) for request in requests]
    for actual, expected in zip(case.workflow.run.call_args_list, requests):
        assert actual.args[0] is expected
    for request, state, snapshot in zip(case.requests, states, snapshots):
        assert request.state is state
        assert isinstance(state, MappingProxyType)
        for name, value in snapshot.items():
            assert getattr(request, name) is value
        if isinstance(request, NodeRuntimeModifierRequest):
            result = results[id(request)]
            assert isinstance(result, NodeRuntimeModifierRequest)
            assert result is not request
            assert not hasattr(request, "context")
            assert not hasattr(result, "context")
            for name in ("workflow_name", "completed_stages", "skipped_stages"):
                assert not hasattr(request, name)
                assert not hasattr(result, name)
            for field in fields(request):
                if field.init:
                    assert getattr(result, field.name) is getattr(request, field.name)
            assert request.test_file_path == request.log_file_path == request.test_log == ""
            assert request.amended_node_file_path == ""
            assert request.test_result is None
            project_root = Path(request.working_directory)
            assert result.test_file_path == str(project_root / "tests" / f"test_{request.node_name}.py")
            assert result.amended_node_file_path == request.node_file_name
            if request.skip_run_node_test:
                assert result.log_file_path == result.test_log == ""
                assert result.test_result is None
            else:
                assert result.log_file_path == str(project_root / "logs" / f"{request.node_name}_test.log")
                assert result.test_log == f"Test output for {request.node_name}"
                assert result.test_result == {"ok": True, "returncode": 0}
        else:
            assert isinstance(request, (
                NodeGenerationRequest, GraphGenerationRequest, MainEntrypointGenerationRequest,
            ))
            if isinstance(request, NodeGenerationRequest):
                _assert_complete_node_request(request)
                assert request.node_meta.name == state["node_name"]
            elif isinstance(request, MainEntrypointGenerationRequest):
                assert not hasattr(request, "temperature")
            with pytest.raises(FrozenInstanceError):
                setattr(request, "state", {})
        state["callback"].assert_not_called()
        assert state["build_session"].graph_plan_path is None
    case.constructor.assert_called_once_with(**case.dependencies)
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


@pytest.mark.parametrize("workflow_service", [_WORKFLOW_SERVICE_CASES[2]], indirect=True, ids=("artifact",))
def test_node_artifact_service_preserves_mutable_caller_data_and_independent_run_outputs(
    workflow_service, tmp_path,
) -> None:
    case = workflow_service
    request = case.requests[0]
    request.working_directory = str(tmp_path / "unused" / "..")
    request.test_file_path = str(tmp_path / "previous_test.py")
    request.log_file_path = str(tmp_path / "previous_test.log")
    request.test_log = "previous output"
    request.test_result = {"ok": False}
    request.amended_node_file_path = str(tmp_path / "previous_node.py")
    Path(request.test_file_path).write_text("# previous test\n", encoding="utf-8")
    Path(request.log_file_path).write_text(request.test_log, encoding="utf-8")
    Path(request.amended_node_file_path).write_text("# previous node\n", encoding="utf-8")
    snapshot = {field.name: getattr(request, field.name) for field in fields(request)}
    test_path, log_path, node_path = (
        tmp_path / "current_test.py", tmp_path / "current_test.log", tmp_path / "current_node.py",
    )
    expected_test_result = {"ok": True, "returncode": 0}

    def repair(actual):
        assert actual is request
        result = replace(actual, working_directory=str(Path(actual.working_directory).resolve()))
        assert result.test_file_path == result.log_file_path == result.test_log == ""
        assert result.amended_node_file_path == ""
        assert result.test_result is None
        test_path.write_text("# current test\n", encoding="utf-8")
        log_path.write_text("current output", encoding="utf-8")
        node_path.write_text("# current node\n", encoding="utf-8")
        result.test_file_path = str(test_path)
        result.log_file_path = str(log_path)
        result.test_log = log_path.read_text(encoding="utf-8")
        result.test_result = dict(expected_test_result)
        result.amended_node_file_path = str(node_path)
        return result

    case.workflow.run.side_effect = repair
    first = case.service.run(request)
    second = case.service.run(request)

    assert isinstance(first, NodeRuntimeModifierRequest)
    assert isinstance(second, NodeRuntimeModifierRequest)
    assert len({id(request), id(first), id(second)}) == 3
    for result in (first, second):
        assert not hasattr(result, "context")
        for name in ("workflow_name", "completed_stages", "skipped_stages"):
            assert not hasattr(request, name)
            assert not hasattr(result, name)
        assert result.working_directory == str(tmp_path.resolve())
        for field in fields(request):
            if field.init and field.name != "working_directory":
                assert getattr(result, field.name) is getattr(request, field.name)
        assert result.test_file_path == str(test_path)
        assert result.log_file_path == str(log_path)
        assert result.test_log == "current output"
        assert result.amended_node_file_path == str(node_path)
        assert result.test_result == expected_test_result
        assert result.test_result is not request.test_result
        assert result.test_result is not expected_test_result
    assert first.test_result is not second.test_result
    first.test_result["first_only"] = True
    first.test_file_path = str(tmp_path / "first_only_test.py")
    first.log_file_path = str(tmp_path / "first_only_test.log")
    first.test_log = "first only output"
    first.amended_node_file_path = str(tmp_path / "first_only_node.py")
    assert second.test_result == expected_test_result
    assert second.test_file_path == str(test_path)
    assert second.log_file_path == str(log_path)
    assert second.test_log == "current output"
    assert second.amended_node_file_path == str(node_path)
    for name, value in snapshot.items():
        assert getattr(request, name) is value
    assert request.test_result == {"ok": False}
    assert Path(request.test_file_path).read_text(encoding="utf-8") == "# previous test\n"
    assert Path(request.log_file_path).read_text(encoding="utf-8") == request.test_log == "previous output"
    assert Path(request.amended_node_file_path).read_text(encoding="utf-8") == "# previous node\n"
    case.workflow.run.assert_has_calls([call(request), call(request)])
    assert case.workflow.run.call_count == 2
    case.constructor.assert_called_once_with(**case.dependencies)
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


def test_services_delegate_concurrent_requests_to_the_same_workflow(workflow_service) -> None:
    case = workflow_service
    results = {id(request): result for request, result in zip(case.requests, case.results)}
    entered = Barrier(len(case.requests))

    def run(request):
        # A service-level execution lock would prevent all requests reaching this barrier.
        entered.wait(timeout=_THREAD_TIMEOUT)
        return results[id(request)]

    case.workflow.run.side_effect = run
    with ThreadPoolExecutor(max_workers=len(case.requests)) as executor:
        futures = [executor.submit(case.service.run, request) for request in case.requests]
        for future, expected in zip(futures, case.results):
            assert future.result(timeout=_THREAD_TIMEOUT) is expected
    assert case.workflow.run.call_count == len(case.requests)
    assert {id(actual.args[0]) for actual in case.workflow.run.call_args_list} == set(results)
    assert all(len(actual.args) == 1 and not actual.kwargs for actual in case.workflow.run.call_args_list)
    case.constructor.assert_called_once_with(**case.dependencies)
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


def test_services_delegate_repeated_close_without_replacing_terminal_workflows(workflow_service) -> None:
    case = workflow_service
    closed = RuntimeError("workflow is closed")

    def close():
        case.workflow.run.side_effect = closed

    # Workflow lifecycle and native pool behavior are covered by the workflow tests.
    case.workflow.close.side_effect = close
    assert case.service.close() is None
    assert case.service.close() is None
    assert case.workflow.close.call_args_list == [call(), call()]
    with pytest.raises(RuntimeError) as raised:
        case.service.run(case.requests[0])
    assert raised.value is closed
    case.workflow.run.assert_called_once_with(case.requests[0])
    case.constructor.assert_called_once_with(**case.dependencies)
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


@pytest.mark.parametrize("operation", ("run", "close"))
def test_services_propagate_workflow_failures_unchanged(workflow_service, operation) -> None:
    case = workflow_service
    failure = RuntimeError(f"{operation} failed")
    getattr(case.workflow, operation).side_effect = failure
    arguments = (case.requests[0],) if operation == "run" else ()
    with pytest.raises(RuntimeError) as raised:
        getattr(case.service, operation)(*arguments)
    assert raised.value is failure
    getattr(case.workflow, operation).assert_called_once_with(*arguments)
    case.constructor.assert_called_once_with(**case.dependencies)
    _assert_dependency_only_storage(case.service, case.dependencies, case.workflow)


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
def test_agent_builder_close_delegates_to_all_four_services_even_if_cleanup_fails(failed_kind) -> None:
    builder = SimpleNamespace(**{
        name: Mock(name=name, spec_set=["close"]) for _, _, _, _, name in _WORKFLOW_SERVICE_CASES
    })
    closes = Mock()
    failure = RuntimeError("service cleanup failed")
    for kind, _, _, _, name in _WORKFLOW_SERVICE_CASES:
        service = getattr(builder, name)
        closes.attach_mock(service.close, kind)
        if kind == failed_kind:
            service.close.side_effect = failure
    if failed_kind is None:
        assert AgentBuilder.close(builder) is None
    else:
        with pytest.raises(RuntimeError) as raised:
            AgentBuilder.close(builder)
        assert raised.value is failure
    assert closes.mock_calls == [getattr(call, case[0])() for case in reversed(_WORKFLOW_SERVICE_CASES)]
    for _, _, _, _, name in _WORKFLOW_SERVICE_CASES:
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
        for kind, _, _, _, name in _WORKFLOW_SERVICE_CASES:
            assert getattr(builder, name).workflow is workflows[kind]
        return runtime

    runtime_constructor = Mock(side_effect=construct_runtime)
    monkeypatch.setattr(agent_builder_module, "RuntimeService", runtime_constructor)

    builder._init_services()

    for kind, service_class, _, _, name in _WORKFLOW_SERVICE_CASES:
        constructors[kind].assert_called_once_with(**expected[kind])
        assert isinstance(getattr(builder, name), service_class)
        _assert_dependency_only_storage(getattr(builder, name), expected[kind], workflows[kind])
        workflows[kind].run.assert_not_called()
        workflows[kind].close.assert_not_called()
    runtime_constructor.assert_called_once_with(
        logger=builder._logger, select_python_command=builder._select_python_command,
        popen_subprocess=builder._popen_subprocess, run_subprocess=builder._run_subprocess,
        timeout_expired=TimeoutError,
    )
    assert builder._runtime_service is runtime


@pytest.mark.parametrize("failed_index", range(len(_WORKFLOW_SERVICE_CASES)), ids=("graph", "node", "artifact", "main"))
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
    failed_kind = _WORKFLOW_SERVICE_CASES[failed_index][0]
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
    for index, (kind, _, _, _, name) in enumerate(_WORKFLOW_SERVICE_CASES):
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
        getattr(call, case[0])() for case in reversed(_WORKFLOW_SERVICE_CASES[:failed_index])
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
    assert builder.node_doc_paths == []
    builder._node_build_service.run.assert_not_called()
    builder._node_artifact_service.run.assert_not_called()


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
    builder._node_build_service.run.assert_not_called()
    builder._node_artifact_service.run.assert_not_called()


@pytest.mark.parametrize("workflow_names", [
    ("default", "default"), ("named", "named"), ("alpha", "beta"),
], ids=("default-nodes", "named-nodes", "different-workflows"))
def test_overlapping_node_repairs_merge_completed_test_paths_and_reuse_them(
    builder, tmp_path, workflow_names,
) -> None:
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": name, "type": "WorkflowStepNode", "desc": name, "depends": []}
        for name in ("First", "Second")
    ]}), encoding="utf-8")
    for name in ("First", "Second"):
        (tmp_path / f"{name}.py").write_text(f"# {name}\n", encoding="utf-8")
    builder.graph_plan_path = str(graph_path)
    builder._load_planned_graph()
    initial = {"unrelated": {"Keep": "preserved.py"}}
    for name in workflow_names:
        initial[name] = {"Existing": f"{name}_existing.py"}
    builder.dynamic_graph_cache["workflow_node_tests"] = initial
    if "default" in workflow_names:
        builder.dynamic_graph_cache["node_tests"] = dict(initial["default"])
    entered = {name: Event() for name in ("First", "Second")}
    finish = {name: Event() for name in entered}
    paths = {
        name: str(tmp_path / "custom_tests" / f"{workflow}_{name}.py")
        for workflow, name in zip(workflow_names, entered)
    }

    def repair(request):
        assert request.state["build_session"] is builder._session
        workflow = workflow_names[("First", "Second").index(request.node_name)]
        assert not hasattr(request, "workflow_name")
        assert request.node_file_name == str(tmp_path / f"{request.node_name}.py")
        assert request.workflow_graph == str(graph_path)
        assert request.working_directory == str(tmp_path)
        assert request.log_file_name == (
            "" if workflow == "default" else str(Path("logs") / workflow / f"{request.node_name}_test.log")
        )
        if not request.skip_generate_node_test:
            assert request.test_file_name == (
                "" if workflow == "default" else str(Path("tests") / workflow / f"test_{request.node_name}.py")
            )
            entered[request.node_name].set()
            assert finish[request.node_name].wait(_THREAD_TIMEOUT), "test must release each repair"
        else:
            assert request.test_file_name == paths[request.node_name]
        test_path = Path(paths[request.node_name])
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("# completed test\n", encoding="utf-8")
        result = replace(request)
        result.test_file_path = str(test_path)
        return result

    builder._node_artifact_service.run.side_effect = repair
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            builder.run_node_modifier, workflow_name=workflow_names[0], node_name="First",
            graph_plan_path=str(graph_path), run_test=False, amend_node=False,
        )
        second = executor.submit(
            builder.run_node_modifier, workflow_name=workflow_names[1], node_name="Second",
            graph_plan_path=str(graph_path), run_test=False, amend_node=False,
        )
        try:
            assert entered["First"].wait(_THREAD_TIMEOUT)
            assert entered["Second"].wait(_THREAD_TIMEOUT), "execution must not hold a builder publication lock"
            finish["First"].set()
            first_result = first.result(timeout=_THREAD_TIMEOUT)
            assert isinstance(first_result, NodeRuntimeModifierRequest)
            assert first_result.test_file_path == paths["First"]
            assert builder.dynamic_graph_cache["workflow_node_tests"][workflow_names[0]]["First"] == paths["First"]
            finish["Second"].set()
            second_result = second.result(timeout=_THREAD_TIMEOUT)
            assert isinstance(second_result, NodeRuntimeModifierRequest)
            assert second_result.test_file_path == paths["Second"]
        finally:
            finish["First"].set()
            finish["Second"].set()

    requests = {
        actual.args[0].node_name: actual.args[0]
        for actual in builder._node_artifact_service.run.call_args_list
    }
    for result in (first_result, second_result):
        request = requests[result.node_name]
        assert result is not request
        assert not hasattr(result, "context")
        assert result.state is request.state
        for field in fields(request):
            if field.init:
                assert getattr(result, field.name) is getattr(request, field.name)
        for name in ("workflow_name", "completed_stages", "skipped_stages"):
            assert not hasattr(request, name)
            assert not hasattr(result, name)
        assert request.test_file_path == request.log_file_path == request.test_log == ""
        assert request.amended_node_file_path == ""
        assert request.test_result is None
        assert Path(result.test_file_path).read_text(encoding="utf-8") == "# completed test\n"
        assert result.log_file_path == result.test_log == ""
        assert result.amended_node_file_path == ""
        assert result.test_result is None
    published = builder.dynamic_graph_cache["workflow_node_tests"]
    assert published["unrelated"] == {"Keep": "preserved.py"}
    for workflow, name in zip(workflow_names, ("First", "Second")):
        assert published[workflow][name] == paths[name]
        assert published[workflow]["Existing"] == f"{workflow}_existing.py"
        reused = builder.run_node_modifier(
            workflow_name=workflow, node_name=name, graph_plan_path=str(graph_path),
            generate_test=False, run_test=False, amend_node=False,
        )
        assert isinstance(reused, NodeRuntimeModifierRequest)
        assert reused.test_file_path == paths[name]
        request = builder._node_artifact_service.run.call_args.args[0]
        assert reused is not request
        assert reused.state is request.state
        assert request.test_file_path == ""
    if "default" in workflow_names:
        assert builder.dynamic_graph_cache["node_tests"] == published["default"]
    assert initial == {
        "unrelated": {"Keep": "preserved.py"},
        **{name: {"Existing": f"{name}_existing.py"} for name in workflow_names},
    }


def test_default_repair_publication_preserves_legacy_test_path_fallback(builder, tmp_path) -> None:
    graph_path = _generation_files(builder, tmp_path)
    (tmp_path / "Shared.py").write_text("# node\n", encoding="utf-8")
    builder.dynamic_graph_cache["node_tests"] = {"Legacy": "legacy_custom_test.py"}

    def repair(request):
        result = replace(request)
        result.test_file_path = str(tmp_path / "custom_test.py")
        return result

    builder._node_artifact_service.run.side_effect = repair
    result = builder.run_node_modifier(
        node_name="Shared", graph_plan_path=str(graph_path), run_test=False, amend_node=False,
    )

    request = builder._node_artifact_service.run.call_args.args[0]
    assert isinstance(result, NodeRuntimeModifierRequest)
    assert result is not request
    assert not hasattr(result, "context")
    assert result.state is request.state
    assert request.test_file_path == ""

    expected = {"Legacy": "legacy_custom_test.py", "Shared": str(tmp_path / "custom_test.py")}
    assert builder.dynamic_graph_cache["node_tests"] == expected
    assert builder.dynamic_graph_cache["workflow_node_tests"]["default"] == expected


def test_agent_builder_graph_facade_delegates_and_publishes_shared_artifacts(builder, tmp_path) -> None:
    planned_path, amended_path = str(tmp_path / "planned.json"), str(tmp_path / "amended.json")
    existing_requirement = str(tmp_path / "existing.md")
    planned_graph, amended_graph = object(), object()
    builder._instantiate_graph.side_effect = [planned_graph, amended_graph]
    builder._graph_build_service.run.side_effect = [
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
    assert builder.amend_graph("Add validation", temperature=0.55) == amended_path
    assert builder._session.graph_plan_path == amended_path
    assert builder._session.planned_graph is amended_graph
    assert builder.analyze_requirement(requirement_file=existing_requirement) == existing_requirement
    assert builder._session.requirement_md_path == existing_requirement
    assert builder._session.graph_plan_path == amended_path
    assert builder._session.planned_graph is amended_graph
    assert builder._instantiate_graph.call_args_list == [call(planned_path), call(amended_path)]
    state = {"build_session": builder._session}
    assert builder._graph_build_service.run.call_args_list == [
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
    for actual in builder._graph_build_service.run.call_args_list:
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

    builder._graph_build_service.run.side_effect = run
    expected_graph = requested_graph if skip_plan and skip_audit else (planned if skip_audit else audited)

    def instantiate(path):
        assert path == builder.graph_plan_path == expected_graph
        assert builder.planned_graph is None
        return graph

    builder._instantiate_graph.side_effect = instantiate

    assert builder._run_graph_request(request) is result

    builder._graph_build_service.run.assert_called_once()
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

    builder._graph_build_service.run.side_effect = fail
    with pytest.raises(RuntimeError) as raised:
        builder._run_graph_request(request)
    assert raised.value is failure
    assert builder.planned_graph is (None if invalidates else old_graph)
    builder._graph_build_service.run.assert_called_once()
    builder._instantiate_graph.assert_not_called()


def test_agent_builder_graph_instantiation_failure_cannot_restore_the_old_topology(builder) -> None:
    builder.graph_plan_path = "existing.json"
    builder.planned_graph = object()
    builder._graph_build_service.run.return_value = SimpleNamespace(outputs={
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


def test_agent_builder_rerun_publishes_process_and_runtime_data(builder, tmp_path) -> None:
    main_path = str(tmp_path / "main.py")
    artifacts = {"main_entrypoint": main_path, "backend_nodes": {"Extract": "Extract.py"}}
    previous_process, process = Mock(), object()
    backend = {"pid": 123, "command": ["/custom/python", main_path], "cwd": str(tmp_path)}
    builder.backend_server_process = previous_process
    builder._validate_generated_artifacts = Mock(return_value=artifacts)
    builder._runtime_service.rerun_server.return_value = (process, backend)

    result = builder.rerun_server(
        graph_plan_path="workflow.json", node_docs_dirname="docs", backend_language="python",
        main_entrypoint_path=main_path, backend_port=9000,
    )

    builder._validate_generated_artifacts.assert_called_once_with(
        graph_plan_path="workflow.json", node_docs_dirname="docs", backend_language="python",
        main_entrypoint_path=main_path,
    )
    builder._runtime_service.rerun_server.assert_called_once_with(
        main_entrypoint_path=main_path, previous_process=previous_process,
    )
    assert result == {"backend": backend, "artifacts": artifacts}
    assert result["backend"] is backend
    assert result["artifacts"] is artifacts
    assert builder._session.backend_server_process is process
    assert builder._session.runtime.dynamic_graph_cache["server_runtime"] is result


def test_agent_builder_runtime_failure_preserves_previously_published_runtime(builder) -> None:
    previous_process, previous_runtime = object(), {"backend": {"pid": 123}}
    builder.backend_server_process = previous_process
    builder.dynamic_graph_cache["server_runtime"] = previous_runtime
    builder._validate_generated_artifacts = Mock(return_value={"main_entrypoint": "main.py"})
    failure = RuntimeError("server failed to start")
    builder._runtime_service.rerun_server.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        builder.rerun_server()
    assert raised.value is failure
    builder._runtime_service.rerun_server.assert_called_once_with(
        main_entrypoint_path="main.py", previous_process=previous_process,
    )
    assert builder.backend_server_process is previous_process
    assert builder.dynamic_graph_cache["server_runtime"] is previous_runtime


@pytest.mark.parametrize("audit_ok", (True, False), ids=("audit-passed", "audit-failed"))
@pytest.mark.parametrize("load_graph", (False, True), ids=("existing-graph", "explicit-graph"))
def test_agent_builder_main_test_publishes_log_path_then_audits_and_amends_in_the_builder(
    builder, tmp_path, audit_ok, load_graph,
) -> None:
    main_path, graph_path = str(tmp_path / "main.py"), str(tmp_path / "workflow.json")
    log_path = str(tmp_path / "logs" / "main_test.txt")
    builder.graph_plan_path = "existing.json"
    builder.requirement_md_path = "requirement.md"
    builder._load_planned_graph = Mock(side_effect=lambda path: setattr(builder, "graph_plan_path", path))
    builder._runtime_service.test_main_entrypoint.return_value = None
    violation = SimpleNamespace(rule="main.py", detail="Fix the import")
    builder.output_auditor.audit_log_file.return_value = (audit_ok, [] if audit_ok else [violation])
    coder = Mock(spec_set=["amend_code_with_feedback"])
    builder._resolve_amendment_target = Mock(return_value=(coder, main_path))
    operations = Mock()
    operations.attach_mock(builder._load_planned_graph, "load_graph")
    operations.attach_mock(builder._runtime_service.test_main_entrypoint, "execute")
    operations.attach_mock(builder.output_auditor.audit_log_file, "audit")

    assert builder.test_main_entrypoint(
        main_path, log_filename="logs/main_test.txt", graph_plan_path=graph_path if load_graph else None,
    ) is audit_ok

    assert builder._session.log_path == log_path
    assert operations.mock_calls == ([call.load_graph(graph_path)] if load_graph else []) + [
        call.execute(main_entrypoint_path=main_path, log_path=log_path), call.audit(log_path),
    ]
    if audit_ok:
        builder._resolve_amendment_target.assert_not_called()
        coder.amend_code_with_feedback.assert_not_called()
    else:
        builder._resolve_amendment_target.assert_called_once_with("main.py", language="python")
        coder.amend_code_with_feedback.assert_called_once_with(
            main_path, violation.detail, graph_plan_path=graph_path if load_graph else "existing.json",
            requirement_md_path="requirement.md", current_node_name="main", language="python", temperature=0.3,
        )


def test_agent_builder_main_facade_seeds_session_and_publishes_normalized_output(builder, tmp_path) -> None:
    graph_path = str(tmp_path / "workflow.json")
    normalized_path = str(tmp_path / "normalized_main.py")
    builder._main_entrypoint_service.run.return_value = normalized_path

    assert builder.generate_main_entrypoint(
        graph_path, output_filename="entrypoints/main.py", fastapi_host="127.0.0.1",
        temperature=0.4, fastapi_port=9000,
    ) == normalized_path

    builder._main_entrypoint_service.run.assert_called_once_with(MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=graph_path,
        output_path=str(tmp_path / "entrypoints" / "main.py"), fastapi_host="127.0.0.1",
        fastapi_port=9000, state={"build_session": builder._session},
    ))
    forwarded = builder._main_entrypoint_service.run.call_args.args[0]
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