"""Test explicit node requests, repair routing, and workflow lifecycle cleanup."""
# Modified 2026-10-14: explicitly inject replacement components into builder services.
# Modified 2026-10-14: exercise eager workflows with offline request-local inputs.
# Modified 2026-10-15: verify flat generation batches and fresh repair request outputs.
# Modified 2026-10-15: verify minimal request APIs and builder-local workflow routing.
# Modified 2026-10-15: capture complete per-node requests and builder-owned batch publications.
# Modified 2026-10-15: reject batch generation and separate explicit node requests from lazy log repair.

import json
from dataclasses import dataclass, field, fields
from inspect import Parameter, signature
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ag_ui_workflow import StepRunOutput, WorkflowStepNode
import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.architect.graph import NodeMeta
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.builder_services import (
    MainEntrypointService,
    NodeArtifactService,
    NodeBuildService,
    RuntimeService,
)
from flowx_core.builder_support import WorkflowArtifactState
from flowx_core.tools._node_settings import apply_node_settings
from flowx_core.tools.node_formats import collect_node_formats
from flowx_core.worker.node_writer import WorkflowStepNodeCoder
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.workflows.main_entrypoint_generation import MainEntrypointGenerationRequest
from flowx_core.workflows.node_generation import NodeGenerationRequest
from flowx_core.workflows.node_runtime_modifier import NodeRuntimeModifierRequest
import flowx_core.workflows.node_runtime_modifier as node_runtime_modifier_module


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeService:
    def __init__(self, **_kwargs):
        self.workflow = Mock(spec=["run", "close"])
        self.workflow.run.side_effect = AssertionError("unused workflow must not run")
        self._closed = False

    def run(self, request):
        return self.workflow.run(request)

    def close(self):
        if not self._closed:
            self._closed = True
            self.workflow.close()


@dataclass
class _FakeWriter(WorkflowStepNodeCoder):
    write_calls: list = field(default_factory=list)
    amend_calls: list = field(default_factory=list)
    __hash__ = object.__hash__

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)


class _FakePlanner(NodePlanElement):
    __hash__ = object.__hash__

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def code_to_file(self, prompt, path, **kwargs):
        Path(path).write_text("# Node plan", encoding="utf-8")
        return Path(path)


class _FakeAuditor(NodeAuditor):
    def audit_file(self, file_path, node_meta=None, graph_plan_path=None):
        return True, []


@dataclass
class _FakeTestWriter(node_runtime_modifier_module.GenerateNodeTestFile):
    write_calls: list = field(default_factory=list)
    __hash__ = object.__hash__

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def write_test_from_node_file(self, node_file_path, output_path, **kwargs):
        self.write_calls.append((node_file_path, output_path, kwargs))
        path = Path(output_path)
        path.write_text("def test_node():\n    assert True\n", encoding="utf-8")
        return path


@dataclass
class _FakeModifierWriter(node_runtime_modifier_module.AmendNodeFromTestLog):
    amend_calls: list = field(default_factory=list)
    __hash__ = object.__hash__

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def amend_code_with_feedback(self, code_path, amendment, **kwargs):
        self.amend_calls.append((code_path, amendment, kwargs))
        path = Path(code_path)
        path.write_text("# repaired\n", encoding="utf-8")
        return path


def _make_builder(monkeypatch, tmp_path: str | Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakePlanner)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    for name in ("RequestNodeFileCoder", "GenerateNodeTestFile", "AmendNodeFromTestLog"):
        monkeypatch.setattr(agent_builder_module, name, _FakeComponent)
    for name in ("GraphBuildService", "NodeBuildService", "NodeArtifactService", "MainEntrypointService"):
        monkeypatch.setattr(agent_builder_module, name, _FakeService)
    return AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )


def _replace_node_build_service(builder, node_coder) -> NodeBuildService:
    builder._node_build_service.close()
    builder.node_coder = node_coder
    builder.node_auditor = _FakeAuditor()
    builder._node_build_service = NodeBuildService(
        node_planner=builder.node_planner,
        node_coder=builder.node_coder,
        node_auditor=builder.node_auditor,
    )
    return builder._node_build_service


def _replace_node_artifact_service(
    builder, *, node_test_writer, node_writer, run_subprocess,
) -> NodeArtifactService:
    builder._node_artifact_service.close()
    builder.node_test_writer = node_test_writer
    builder.node_modifier_writer = node_writer
    builder._node_artifact_service = NodeArtifactService(
        node_test_writer=node_test_writer,
        node_writer=node_writer,
        run_subprocess=run_subprocess,
        timeout_expired=builder._subprocess_timeout_expired(),
    )
    return builder._node_artifact_service


def _assert_no_request_cache(service):
    def contains_request(value):
        if isinstance(value, (NodeGenerationRequest, NodeRuntimeModifierRequest)):
            return True
        if isinstance(value, dict):
            return any(contains_request(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(contains_request(item) for item in value)
        return False

    for owner in (service, service.workflow):
        assert not any(contains_request(value) for value in vars(owner).values())


def _assert_fresh_modifier_request(request, result):
    assert isinstance(request, NodeRuntimeModifierRequest)
    assert isinstance(result, NodeRuntimeModifierRequest)
    assert result is not request
    assert not hasattr(request, "context")
    assert not hasattr(result, "context")
    for request_field in fields(NodeRuntimeModifierRequest):
        if request_field.init and request_field.name != "working_directory":
            assert getattr(result, request_field.name) == getattr(request, request_field.name)
    assert result.working_directory == str(Path(request.working_directory).expanduser().resolve())
    assert result.state is request.state
    assert request.test_file_path == request.log_file_path == request.test_log == ""
    assert request.test_result is None
    assert request.amended_node_file_path == ""
    for removed_field in ("workflow_name", "skipped_stages", "completed_stages"):
        assert not hasattr(request, removed_field)
        assert not hasattr(result, removed_field)


def test_builder_close_releases_all_four_initialized_workflows(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    workflows = [
        service.workflow for service in (
            builder._graph_build_service,
            builder._node_build_service,
            builder._node_artifact_service,
            builder._main_entrypoint_service,
        )
    ]
    builder.close()
    builder.close()
    assert len({id(workflow) for workflow in workflows}) == 4
    for workflow in workflows:
        workflow.close.assert_called_once_with()


def test_builder_session_splits_artifact_and_runtime_state(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    process = object()
    builder.graph_plan_path = str(tmp_path / "workflow.json")
    builder.node_docs_dir = str(tmp_path / "node_docs")
    builder.log_path = str(tmp_path / "runtime.log")
    builder.backend_server_process = process
    builder.dynamic_graph_cache["node_tests"] = {"NodeA": "test_NodeA.py"}
    builder.dynamic_graph_cache["server_runtime"] = {"pid": 99}

    assert builder.artifact_state.graph_plan_path == str(tmp_path / "workflow.json")
    assert builder.artifact_state.node_docs_dir == str(tmp_path / "node_docs")
    assert builder.runtime_state.log_path == str(tmp_path / "runtime.log")
    assert builder.runtime_state.backend_server_process is process
    assert builder.artifact_state.dynamic_graph_cache["node_tests"] == {"NodeA": "test_NodeA.py"}
    assert builder.runtime_state.dynamic_graph_cache["server_runtime"] == {"pid": 99}
    builder.close()


def _write_graph(graph_path: Path, nodes: list[dict]) -> None:
    graph_path.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_generate_nodes_retains_public_signature():
    assert not hasattr(AgentBuilder, "_generate_nodes")
    parameters = signature(AgentBuilder.generate_nodes).parameters

    assert list(parameters) == [
        "self", "graph_plan_path", "requirement_md_path", "language", "temperature",
        "node_names", "run_nodes", "generate_markdowns", "markdown_amendments",
        "node_amendments", "workflow_name",
    ]
    assert all(parameter.kind is Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters.values())
    assert parameters["self"].default is Parameter.empty
    assert {name: parameter.default for name, parameter in parameters.items() if name != "self"} == {
        "graph_plan_path": None,
        "requirement_md_path": None,
        "language": "python",
        "temperature": 0.35,
        "node_names": None,
        "run_nodes": None,
        "generate_markdowns": False,
        "markdown_amendments": None,
        "node_amendments": None,
        "workflow_name": "default",
    }


@pytest.mark.parametrize(("args", "kwargs"), [
    pytest.param((), {}, id="no-requirement-or-graph"),
    pytest.param((), {
        "graph_plan_path": "missing/workflow.json",
        "requirement_md_path": "missing/requirement.md",
        "node_names": ["MissingNode"],
        "run_nodes": {"MissingNode": False},
        "generate_markdowns": True,
        "workflow_name": "",
    }, id="invalid-inputs-are-not-validated"),
    pytest.param((
        "missing/workflow.json", "missing/requirement.md", "python", 0.1,
        ["MissingNode"], ["MissingNode"], True,
        {"MissingNode": "Revise brief"}, {"MissingNode": "Revise code"}, "billing",
    ), {}, id="legacy-positional-arguments"),
])
def test_generate_nodes_immediately_rejects_batch_generation(monkeypatch, tmp_path, args, kwargs):
    builder = _make_builder(monkeypatch, tmp_path)
    run_modifier = Mock(side_effect=AssertionError("batch generation must not generate tests"))
    monkeypatch.setattr(builder, "run_node_modifier", run_modifier)
    original_paths = set(tmp_path.rglob("*"))
    original_cache = {name: dict(value) for name, value in builder.dynamic_graph_cache.items()}

    with pytest.raises(NotImplementedError) as exc_info:
        builder.generate_nodes(*args, **kwargs)

    assert str(exc_info.value) == (
        "Batch node generation is not implemented; "
        "submit a NodeGenerationRequest to NodeBuildService.run() instead."
    )
    assert builder.requirement_md_path is None
    assert builder.graph_plan_path is None
    assert builder.planned_graph is None
    assert builder.node_location_map == builder.node_coder_map == {}
    assert builder.node_docs_dir is None
    assert builder.node_doc_paths == []
    assert builder.artifact_state.workflow_artifacts == {}
    assert dict(builder.dynamic_graph_cache) == original_cache
    assert set(tmp_path.rglob("*")) == original_paths
    for service in (
        builder._graph_build_service, builder._node_build_service,
        builder._node_artifact_service, builder._main_entrypoint_service,
    ):
        service.workflow.run.assert_not_called()
    run_modifier.assert_not_called()
    builder.close()


def test_node_build_service_generates_and_amends_explicit_requests(monkeypatch, tmp_path):
    project_root = tmp_path / "project_root"
    graph_dir = tmp_path / "graph_dir"
    builder = _make_builder(monkeypatch, project_root)

    requirement_path = graph_dir / "requirement.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = graph_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowStepNode",
                "desc": "existing dependency",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "GeneratedNode",
                "type": "WorkflowStepNode",
                "desc": "generated",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )
    existing_path = graph_dir / "ExistingNode.py"
    existing_source = "class ExistingNode: ...\n# existing dependency\n"
    existing_path.write_text(existing_source, encoding="utf-8")
    docs_dir = project_root / "node_docs"
    docs_dir.mkdir()
    existing_markdown = "# Existing dependency\n"
    (docs_dir / "ExistingNode.md").write_text(existing_markdown, encoding="utf-8")

    @dataclass
    class _FakeNodePlanner(_FakePlanner):
        code_calls: list = field(default_factory=list)
        __hash__ = object.__hash__

        def code_to_file(self, prompt, path, **kwargs):
            self.code_calls.append({"prompt": prompt, "path": path, "kwargs": kwargs})
            target_path = Path(path)
            target_path.write_text(f"# Node plan {len(self.code_calls)}\n", encoding="utf-8")
            return target_path

    class _FakeNodeCoder(_FakeWriter):
        def write_node_from_requirement(self, node_name, node_meta, requirement_md_path, output_path, **kwargs):
            self.write_calls.append({
                "node_name": node_name,
                "node_meta": node_meta.to_dict(),
                "requirement_md_path": requirement_md_path,
                "output_path": output_path,
                "kwargs": kwargs,
            })
            target_path = Path(output_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.suffix != ".py":
                target_path = target_path.with_suffix(".py")
            target_path.write_text(f"class {node_name}: ...\n", encoding="utf-8")
            return str(target_path)

        def amend_code_with_feedback(self, file_path, amendment, **kwargs):
            self.amend_calls.append(
                {
                    "file_path": file_path,
                    "amendment": amendment,
                    "kwargs": kwargs,
                }
            )
            target_path = Path(file_path)
            target_path.write_text(f"# amended\n# {amendment}\n", encoding="utf-8")
            return str(target_path)

    builder.node_planner = _FakeNodePlanner()
    fake_node_coder = _FakeNodeCoder()
    service = _replace_node_build_service(builder, fake_node_coder)
    run_modifier = Mock(side_effect=AssertionError("node generation must not generate tests"))
    monkeypatch.setattr(builder, "run_node_modifier", run_modifier)
    original_cache = {name: dict(value) for name, value in builder.dynamic_graph_cache.items()}
    node_path = (graph_dir / "GeneratedNode.py").resolve()
    markdown_path = (docs_dir / "GeneratedNode.md").resolve()
    raw_node = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"][1]
    dependency_context = (
        "- previously generated node artifacts (use as authoritative dependency/design context):\n"
        f"\n### ExistingNode\nMarkdown:\n{existing_markdown}\nGenerated file:\n{existing_source}"
    )
    request = NodeGenerationRequest(
        graph_plan_path=str(graph_path.resolve()),
        requirement_md_path=str(requirement_path.resolve()),
        requirement_text=requirement_path.read_text(encoding="utf-8"),
        node_meta=NodeMeta.from_dict(raw_node),
        raw_node=raw_node,
        target_path=node_path,
        markdown_path=markdown_path,
        markdown_amendment="Clarify node brief",
        node_amendment="Apply initial guard",
        node_format={
            "user_input_format": {}, "backend_output_card_format": None, "backend_node_path": None,
        },
        dependency_context=dependency_context,
    )
    assert {item.name for item in fields(NodeGenerationRequest)} == {
        "graph_plan_path", "requirement_md_path", "requirement_text", "node_meta",
        "raw_node", "target_path", "markdown_path", "language", "temperature",
        "generate_markdown", "skip_node_planner", "markdown_amendment", "node_amendment",
        "node_format", "dependency_context", "state",
    }
    assert request.skip_node_planner is False

    generated_path = service.run(request)

    assert generated_path == str(node_path)
    assert node_path.read_text(encoding="utf-8") == "class GeneratedNode: ...\n"
    assert markdown_path.read_text(encoding="utf-8") == "# Node plan 2\n"
    amendment_request = NodeGenerationRequest(
        graph_plan_path=request.graph_plan_path,
        requirement_md_path=request.requirement_md_path,
        requirement_text=request.requirement_text,
        node_meta=NodeMeta.from_dict(raw_node),
        raw_node=dict(raw_node),
        target_path=node_path,
        markdown_path=markdown_path,
        temperature=0.1,
        generate_markdown=False,
        markdown_amendment="Revise node brief",
        node_amendment="Apply revised guard",
        node_format={
            "user_input_format": {}, "backend_output_card_format": None,
            "backend_node_path": str(node_path),
        },
        dependency_context=dependency_context,
    )

    amended_path = service.run(amendment_request)

    assert amended_path == generated_path
    assert request.markdown_amendment == "Clarify node brief"
    assert request.node_amendment == "Apply initial guard"
    assert request.node_format["backend_node_path"] is None
    assert request.state == amendment_request.state == {}
    assert request.state is not amendment_request.state
    assert request.dependency_context == amendment_request.dependency_context == dependency_context
    assert len(builder.node_planner.code_calls) == 3
    assert all(call["path"] == str(markdown_path) for call in builder.node_planner.code_calls)
    assert "Clarify node brief" in builder.node_planner.code_calls[1]["prompt"]
    assert "Revise node brief" in builder.node_planner.code_calls[2]["prompt"]
    assert [call["kwargs"]["temperature"] for call in builder.node_planner.code_calls] == [0.2, 0.2, 0.1]
    assert markdown_path.read_text(encoding="utf-8") == "# Node plan 3\n"
    assert len(fake_node_coder.write_calls) == 1
    write_call = fake_node_coder.write_calls[0]
    assert write_call["node_meta"] == request.node_meta.to_dict()
    assert write_call["node_name"] == "GeneratedNode"
    assert write_call["requirement_md_path"] == str(requirement_path.resolve())
    assert write_call["output_path"] == str(node_path)
    assert write_call["kwargs"]["additional_generation_context"] == (
        request.render_generation_context()
        + "\n\nRequested amendment (apply while generating):\nApply initial guard\n"
    )
    assert len(fake_node_coder.amend_calls) == 1
    amend_call = fake_node_coder.amend_calls[0]
    assert amend_call["file_path"] == str(node_path)
    assert amend_call["amendment"] == "Apply revised guard"
    assert amend_call["kwargs"]["current_node_name"] == "GeneratedNode"
    assert amend_call["kwargs"]["temperature"] == 0.1
    assert amend_call["kwargs"]["additional_generation_context"] == amendment_request.render_generation_context()
    assert node_path.read_text(encoding="utf-8") == "# amended\n# Apply revised guard\n"
    assert existing_path.read_text(encoding="utf-8") == existing_source
    assert (docs_dir / "ExistingNode.md").read_text(encoding="utf-8") == existing_markdown
    # Direct service requests do not publish caller-owned builder paths or tests.
    assert builder.requirement_md_path is None
    assert builder.graph_plan_path is None
    assert builder.planned_graph is None
    assert builder.node_location_map == builder.node_coder_map == {}
    assert builder.artifact_state.workflow_artifacts == {}
    assert builder.node_docs_dir is None
    assert builder.node_doc_paths == []
    assert dict(builder.dynamic_graph_cache) == original_cache
    assert not (graph_dir / "tests").exists()
    assert not (project_root / "tests").exists()
    _assert_no_request_cache(service)
    builder._node_artifact_service.workflow.run.assert_not_called()
    run_modifier.assert_not_called()
    builder.close()


def test_lazy_node_coder_can_amend_from_log_prompt(monkeypatch, tmp_path):
    graph_dir = tmp_path / "graph_dir"
    graph_dir.mkdir()
    builder = _make_builder(monkeypatch, tmp_path / "project_root")
    requirement_path = graph_dir / "requirement.md"
    requirement_path.write_text("# Requirement\n", encoding="utf-8")
    graph_path = graph_dir / "workflow.json"
    _write_graph(graph_path, [{
        "name": "GeneratedNode",
        "type": "WorkflowStepNode",
        "desc": "caller-owned node",
        "enable": True,
        "depends": [],
        "ext_data": {"type": "none", "desc": "none"},
    }])
    node_path = graph_dir / "GeneratedNode.py"
    node_path.write_text("class GeneratedNode: ...\n", encoding="utf-8")
    # The caller owns path publication; no generation pipeline is needed for repair.
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)
    builder.node_location_map["GeneratedNode"] = str(node_path)
    assert builder.node_coder_map == {}
    assert builder.planned_graph is None

    class _FakeNodeCoder(_FakeWriter):
        def amend_code_with_feedback(self, file_path, amendment, **kwargs):
            self.amend_calls.append({
                "file_path": file_path, "amendment": amendment, "kwargs": kwargs,
            })
            target_path = Path(file_path)
            target_path.write_text(f"# amended\n# {amendment}\n", encoding="utf-8")
            return str(target_path)

    coder = _FakeNodeCoder()
    make_node_coder = Mock(return_value=coder)
    monkeypatch.setattr(builder, "_make_node_coder", make_node_coder)
    audit_log = Mock(return_value=(False, [SimpleNamespace(
        rule=str(node_path), detail="Traceback from node test log",
    )]))
    monkeypatch.setattr(builder.output_auditor, "audit_log_file", audit_log)
    log_path = graph_dir / "node_test.log"
    log_path.write_text("Traceback from node test log\n", encoding="utf-8")

    assert builder.amend_by_log(str(log_path)) is False

    audit_log.assert_called_once_with(str(log_path))
    make_node_coder.assert_called_once_with(builder.planned_graph.get_node_meta("GeneratedNode"))
    assert builder.node_coder_map == {"GeneratedNode": coder}
    assert builder._ensure_node_coder("GeneratedNode") is coder
    make_node_coder.assert_called_once()
    assert builder.node_location_map == {"GeneratedNode": str(node_path)}
    assert coder.write_calls == []
    assert coder.amend_calls == [
        {
            "file_path": str(node_path),
            "amendment": "Traceback from node test log",
            "kwargs": {
                "graph_plan_path": str(graph_path),
                "requirement_md_path": str(requirement_path),
                "current_node_name": "GeneratedNode",
                "language": "python",
                "temperature": 0.3,
            },
        }
    ]
    assert node_path.read_text(encoding="utf-8") == "# amended\n# Traceback from node test log\n"
    builder._node_build_service.workflow.run.assert_not_called()
    builder._node_artifact_service.workflow.run.assert_not_called()
    builder.close()


def test_node_artifact_service_routes_test_update_by_workflow_and_node_names(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExampleNode",
                "type": "WorkflowStepNode",
                "desc": "example",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )
    node_path = tmp_path / "ExampleNode.py"
    node_path.write_text("# source\n", encoding="utf-8")
    builder.graph_plan_path = str(graph_path)
    builder.node_location_map["ExampleNode"] = str(node_path)

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")
    node_writer = _FakeModifierWriter()
    service = _replace_node_artifact_service(
        builder,
        node_test_writer=_FakeTestWriter(),
        node_writer=node_writer,
        run_subprocess=Mock(return_value=SimpleNamespace(returncode=1, stdout="F\n", stderr="failed\n")),
    )
    workflow = service.workflow
    run_modifier = Mock(wraps=service.run)
    monkeypatch.setattr(service, "run", run_modifier)

    context = builder.run_node_modifier(
        workflow_name="billing",
        node_name="ExampleNode",
        graph_plan_path=str(graph_path),
        timeout=17,
    )

    assert context.test_file_path == str(tmp_path / "tests" / "billing" / "test_ExampleNode.py")
    assert context.log_file_path == str(tmp_path / "logs" / "billing" / "ExampleNode_test.log")
    assert context.test_result["node_name"] == "ExampleNode"
    assert context.test_result["ok"] is False
    assert context.amended_node_file_path == str(node_path)
    assert context.workflow_graph == str(graph_path)
    assert context.working_directory == str(tmp_path)
    run_modifier.assert_called_once()
    request, = run_modifier.call_args.args
    assert not run_modifier.call_args.kwargs
    assert isinstance(request, NodeRuntimeModifierRequest)
    _assert_fresh_modifier_request(request, context)
    assert request.node_name == "ExampleNode"
    assert request.node_file_name == str(node_path)
    assert request.workflow_graph == str(graph_path)
    assert request.working_directory == str(tmp_path)
    assert request.test_file_name == "tests/billing/test_ExampleNode.py"
    assert request.log_file_name == "logs/billing/ExampleNode_test.log"
    assert request.python_command == "/custom/python"
    assert request.timeout == 17
    assert request.skip_generate_node_test is False
    assert request.skip_run_node_test is False
    assert request.skip_amend_node is False
    assert request.state["build_session"] is builder._session
    assert service.workflow is workflow
    _assert_no_request_cache(service)
    assert node_writer.amend_calls[0][0] == str(node_path)
    assert builder.dynamic_graph_cache["workflow_node_tests"] == {
        "billing": {
            "ExampleNode": str(tmp_path / "tests" / "billing" / "test_ExampleNode.py")
        }
    }
    builder.close()


def test_named_node_test_update_uses_its_workflow_graph_and_node_file(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_node = {
        "name": "SharedNode",
        "type": "WorkflowStepNode",
        "desc": "shared",
        "enable": True,
        "depends": [],
        "ext_data": {"type": "none", "desc": "none"},
    }
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()
    alpha_graph = alpha_dir / "workflow.json"
    beta_graph = beta_dir / "workflow.json"
    _write_graph(alpha_graph, [graph_node])
    _write_graph(beta_graph, [graph_node])
    alpha_node = alpha_dir / "SharedNode.py"
    beta_node = beta_dir / "SharedNode.py"
    alpha_node.write_text("# alpha\n", encoding="utf-8")
    beta_node.write_text("# beta\n", encoding="utf-8")

    alpha_doc = alpha_dir / "node_docs" / "SharedNode.md"
    beta_doc = beta_dir / "node_docs" / "SharedNode.md"
    builder.artifact_state.workflow_artifacts["alpha"] = WorkflowArtifactState(
        graph_plan_path=str(alpha_graph),
        node_location_map={"SharedNode": str(alpha_node)},
        node_doc_paths={"SharedNode": str(alpha_doc)},
    )
    builder.artifact_state.workflow_artifacts["beta"] = WorkflowArtifactState(
        graph_plan_path=str(beta_graph),
        node_location_map={"SharedNode": str(beta_node)},
        node_doc_paths={"SharedNode": str(beta_doc)},
    )
    builder.graph_plan_path = str(beta_graph)
    builder.node_location_map["SharedNode"] = str(beta_node)

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")
    service = _replace_node_artifact_service(
        builder,
        node_test_writer=_FakeTestWriter(),
        node_writer=_FakeModifierWriter(),
        run_subprocess=Mock(return_value=SimpleNamespace(returncode=0, stdout="passed\n", stderr="")),
    )
    workflow = service.workflow
    run_modifier = Mock(wraps=service.run)
    monkeypatch.setattr(service, "run", run_modifier)

    alpha_context = builder.run_node_modifier(workflow_name="alpha", node_name="SharedNode")

    first_request = run_modifier.call_args.args[0]
    _assert_fresh_modifier_request(first_request, alpha_context)
    assert first_request.node_file_name == str(alpha_node)
    assert first_request.workflow_graph == str(alpha_graph)
    assert first_request.working_directory == str(alpha_dir)
    assert first_request.test_file_name == "tests/alpha/test_SharedNode.py"
    assert first_request.log_file_name == "logs/alpha/SharedNode_test.log"
    assert alpha_context.test_file_path == str(alpha_dir / "tests" / "alpha" / "test_SharedNode.py")
    assert alpha_context.log_file_path == str(alpha_dir / "logs" / "alpha" / "SharedNode_test.log")
    assert builder.dynamic_graph_cache["workflow_node_tests"]["alpha"] == {
        "SharedNode": alpha_context.test_file_path,
    }

    reused_context = builder.run_node_modifier(
        workflow_name="alpha",
        node_name="SharedNode",
        generate_test=False,
    )

    second_request = run_modifier.call_args.args[0]
    _assert_fresh_modifier_request(second_request, reused_context)
    assert second_request.skip_generate_node_test is True
    assert second_request.skip_run_node_test is False
    assert second_request.workflow_graph == str(alpha_graph)
    assert second_request.node_file_name == str(alpha_node)
    assert second_request.working_directory == str(alpha_dir)
    assert second_request.test_file_name == alpha_context.test_file_path
    assert second_request.log_file_name == "logs/alpha/SharedNode_test.log"
    assert reused_context.test_file_path == alpha_context.test_file_path
    assert reused_context.log_file_path == alpha_context.log_file_path

    beta_context = builder.run_node_modifier(workflow_name="beta", node_name="SharedNode")

    third_request = run_modifier.call_args.args[0]
    _assert_fresh_modifier_request(third_request, beta_context)
    assert third_request.node_file_name == str(beta_node)
    assert third_request.workflow_graph == str(beta_graph)
    assert third_request.working_directory == str(beta_dir)
    assert third_request.test_file_name == "tests/beta/test_SharedNode.py"
    assert third_request.log_file_name == "logs/beta/SharedNode_test.log"
    assert beta_context.test_file_path == str(beta_dir / "tests" / "beta" / "test_SharedNode.py")
    assert beta_context.log_file_path == str(beta_dir / "logs" / "beta" / "SharedNode_test.log")
    assert len({id(request) for request in (
        first_request, second_request, third_request, alpha_context, reused_context, beta_context,
    )}) == 6
    assert alpha_context.workflow_graph == reused_context.workflow_graph == str(alpha_graph)
    assert beta_context.workflow_graph == str(beta_graph)
    assert alpha_context.working_directory == reused_context.working_directory == str(alpha_dir)
    assert beta_context.working_directory == str(beta_dir)
    assert alpha_context.node_name == reused_context.node_name == beta_context.node_name == "SharedNode"
    assert all(context.test_result["ok"] is True for context in (
        alpha_context, reused_context, beta_context,
    ))
    assert alpha_context.amended_node_file_path == reused_context.amended_node_file_path == str(alpha_node)
    assert beta_context.amended_node_file_path == str(beta_node)
    assert builder.dynamic_graph_cache["workflow_node_tests"] == {
        "alpha": {"SharedNode": reused_context.test_file_path},
        "beta": {"SharedNode": beta_context.test_file_path},
    }
    assert service.workflow is workflow
    _assert_no_request_cache(service)
    assert builder.get_node_doc_path("SharedNode", workflow_name="alpha") == str(alpha_doc)
    assert builder.get_node_doc_path("SharedNode", workflow_name="beta") == str(beta_doc)
    assert builder.get_node_doc_path("SharedNode", workflow_name="missing") is None
    assert builder.artifact_state.workflow_artifacts["alpha"].node_location_map == {"SharedNode": str(alpha_node)}
    assert builder.artifact_state.workflow_artifacts["beta"].node_location_map == {"SharedNode": str(beta_node)}
    builder.close()


def test_node_artifact_service_reuses_one_eager_workflow_for_named_node_requests(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    test_writer = _FakeTestWriter()
    run_subprocess = Mock(side_effect=AssertionError("node tests must be skipped"))
    service = _replace_node_artifact_service(
        builder,
        node_test_writer=test_writer,
        node_writer=_FakeModifierWriter(),
        run_subprocess=run_subprocess,
    )
    workflow = service.workflow
    engine = workflow._engine
    assert engine is not None
    contexts = []
    requests = []
    selections = (
        ("orders", "Collect"), ("orders", "Collect"), ("orders", "Publish"), ("billing", "Collect"),
    )
    for workflow_name, node_name in selections:
        workdir = tmp_path / workflow_name
        workdir.mkdir(exist_ok=True)
        node_path = workdir / f"{node_name}.py"
        node_path.write_text("# source\n", encoding="utf-8")
        graph_path = workdir / "workflow.json"
        _write_graph(graph_path, [])
        request = NodeRuntimeModifierRequest(
            node_name=node_name,
            node_file_name=str(node_path),
            workflow_graph=str(graph_path),
            working_directory=str(workdir),
            test_file_name=f"tests/{workflow_name}/test_{node_name}.py",
            log_file_name=f"logs/{workflow_name}/{node_name}_test.log",
            skip_run_node_test=True,
            skip_amend_node=True,
        )
        requests.append(request)
        context = service.run(request)
        contexts.append(context)
        _assert_fresh_modifier_request(request, context)
        assert service.workflow is workflow
        assert workflow._engine is engine
        assert context.node_name == node_name
        assert context.node_file_name == str(node_path)
        assert context.workflow_graph == str(graph_path)
        assert context.working_directory == str(workdir)
        assert context.test_file_path == str(workdir / "tests" / workflow_name / f"test_{node_name}.py")
        assert context.log_file_name == f"logs/{workflow_name}/{node_name}_test.log"
        assert Path(context.test_file_path).is_file()
        assert context.log_file_path == ""
        assert context.test_result is None
        assert context.amended_node_file_path == str(node_path)

    assert len({id(context) for context in contexts}) == 4
    assert len({id(request) for request in (*requests, *contexts)}) == 8
    assert [(Path(context.working_directory).name, context.node_name) for context in contexts] == list(selections)
    assert contexts[0].test_file_path == contexts[1].test_file_path
    assert len({context.test_file_path for context in contexts}) == 3
    for request, context in zip(requests, contexts):
        _assert_fresh_modifier_request(request, context)
        assert request.skip_run_node_test is context.skip_run_node_test is True
        assert request.skip_amend_node is context.skip_amend_node is True
    _assert_no_request_cache(service)
    assert len(test_writer.write_calls) == 4
    assert test_writer.root_dir_path == ""
    run_subprocess.assert_not_called()
    builder.close()


def test_sync_workflow_graph_json_does_not_duplicate_relative_root_dir(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo_resume_workflow"
    project_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    builder = _make_builder(monkeypatch, project_dir.name)

    graph_path = project_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ResumeUploadNode",
                "type": "WorkflowFileNode",
                "desc": "upload",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_file_input", "desc": "upload"},
            }
        ],
    )

    builder.graph_plan_path = str(graph_path)

    synced_path = builder._sync_workflow_graph_json()

    assert Path(synced_path).resolve() == graph_path.resolve()
    assert not (project_dir / project_dir.name / "workflow.json").exists()
    assert builder.workflow_json_path == str(graph_path.resolve())
    builder.close()


def test_write_main_entrypoint_does_not_duplicate_relative_root_dir(monkeypatch, tmp_path):
    project_dir = tmp_path / "demo_resume_workflow"
    project_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    builder = _make_builder(monkeypatch, project_dir.name)

    graph_path = project_dir / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ResumeUploadNode",
                "type": "WorkflowFileNode",
                "desc": "upload",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_file_input", "desc": "upload"},
            }
        ],
    )

    captured = {}

    class _FakeMainWriter(PromptMainFileCoder):
        def process_input(self, _user_input, _dependency_results, session_state):
            request = session_state["main_entrypoint"]
            assert session_state[f"{type(self).__name__}::action"] == "write"
            captured.update(request)
            output_path = Path(request["output_path"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("print('main')\n", encoding="utf-8")
            return StepRunOutput(derived={"main_entrypoint_path": str(output_path)})

    class _FakeMainAuditor(MainEntryPointAuditor):
        def audit_file(self, file_path, nodes_root=None):
            assert Path(file_path).is_file()
            return True, []

    builder.main_writer = _FakeMainWriter()
    builder.main_entry_auditor = _FakeMainAuditor()
    builder._main_entrypoint_service.close()
    builder._main_entrypoint_service = MainEntrypointService(
        main_writer=builder.main_writer,
        main_entry_auditor=builder.main_entry_auditor,
    )
    service = builder._main_entrypoint_service
    run_main = Mock(wraps=service.run)
    monkeypatch.setattr(service, "run", run_main)

    main_path = builder._write_main_entrypoint(
        graph_plan_path=str(graph_path),
        output_filename=str(Path(project_dir.name) / "main.py"),
    )

    assert Path(main_path).resolve() == (project_dir / "main.py").resolve()
    assert Path(captured["output_path"]).resolve() == (project_dir / "main.py").resolve()
    assert not (project_dir / project_dir.name / "main.py").exists()
    assert builder.main_output_path == main_path
    run_main.assert_called_once()
    request, = run_main.call_args.args
    assert not run_main.call_args.kwargs
    assert isinstance(request, MainEntrypointGenerationRequest)
    assert request.graph_plan_json_path == str(graph_path)
    assert request.output_path == main_path
    assert request.state["build_session"] is builder._session
    assert service.workflow.main_writer is builder.main_writer
    assert service.workflow.main_entry_auditor is builder.main_entry_auditor
    builder.close()


def test_collect_node_formats_collects_inputs_and_backend_card_schema(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "CollectInput",
                "type": "WorkflowStepNode",
                "desc": "collect user input",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "user_input", "desc": "collect input"},
                "inputs_format": {"query": "String", "limit": "NUMBER"},
            },
            {
                "name": "Summarize",
                "type": "WorkflowStepNode",
                "desc": "summarize results",
                "enable": True,
                "depends": ["CollectInput"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )

    (tmp_path / "CollectInput.py").write_text(
        "class CollectInput(WorkflowStepNode):\n"
        "    STEP_ID = 'CollectInput'\n"
        "    TITLE = 'Collect Input'\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        return StepRunOutput(card={'kind': 'summary', 'fields': [{'name': 'query', 'type': 'string'}]})\n",
        encoding="utf-8",
    )
    (tmp_path / "Summarize.py").write_text(
        "class Summarize(WorkflowStepNode):\n"
        "    STEP_ID = 'Summarize'\n"
        "    TITLE = 'Summarize'\n"
        "    INPUT_REQUIRED = False\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        return StepRunOutput(card={'kind': 'report', 'status': 'done'})\n",
        encoding="utf-8",
    )

    builder.graph_plan_path = str(graph_path)

    formats = collect_node_formats(builder, builder._load_planned_graph())

    assert formats == {
        "CollectInput": {
            "user_input_format": {"query": "string", "limit": "number"},
            "backend_output_card_format": {
                "kind": "summary",
                "fields": [{"name": "query", "type": "string"}],
            },
            "backend_node_path": str(tmp_path / "CollectInput.py"),
        },
        "Summarize": {
            "user_input_format": {},
            "backend_output_card_format": {"kind": "report", "status": "done"},
            "backend_node_path": str(tmp_path / "Summarize.py"),
        },
    }
    builder.close()


def test_rerun_server_validates_artifacts_and_restarts_processes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    graph_path = tmp_path / "graph_plan.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowStepNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            }
        ],
    )

    (tmp_path / "node_docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "node_docs" / "ExistingNode.md").write_text("# ExistingNode\n", encoding="utf-8")
    (tmp_path / "ExistingNode.py").write_text("class ExistingNode: ...\n", encoding="utf-8")
    main_path = tmp_path / "main.py"
    main_path.write_text("print('main')\n", encoding="utf-8")

    class _RunningProcess:
        def __init__(self, pid=1):
            self.pid = pid
            self.terminated = False
            self.killed = False
            self._running = True

        def poll(self):
            return None if self._running else 0

        def terminate(self):
            self.terminated = True
            self._running = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.killed = True
            self._running = False

    spawned = []

    class _SpawnedProcess(_RunningProcess):
        next_pid = 100

        def __init__(self, command, cwd=None, env=None):
            super().__init__(pid=_SpawnedProcess.next_pid)
            _SpawnedProcess.next_pid += 1
            self.command = command
            self.cwd = cwd
            self.env = env
            spawned.append(self)

    def fake_popen(command, cwd=None, env=None):
        return _SpawnedProcess(command, cwd=cwd, env=env)

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/usr/bin/python3.10")

    old_backend = _RunningProcess(pid=11)
    builder.backend_server_process = old_backend
    builder.graph_plan_path = str(graph_path)
    builder.main_output_path = str(main_path)

    monkeypatch.setattr(agent_builder_module.subprocess, "Popen", fake_popen)
    runtime = builder.rerun_server(backend_port=9001)

    assert old_backend.terminated is True
    assert len(spawned) == 1
    assert runtime["backend"]["pid"] == spawned[0].pid
    assert spawned[0].command == ["/usr/bin/python3.10", str(main_path)]
    assert spawned[0].cwd == str(tmp_path)
    assert runtime["artifacts"]["backend_nodes"] == {"ExistingNode": str(tmp_path / "ExistingNode.py")}
    assert isinstance(builder._runtime_service, RuntimeService)
    assert builder.backend_server_process is spawned[0]
    assert builder.dynamic_graph_cache["server_runtime"] == runtime
    builder.close()


def test_test_main_entrypoint_uses_selected_python_command(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)

    main_path = tmp_path / "main.py"
    main_path.write_text("print('main')\n", encoding="utf-8")

    observed: dict[str, object] = {}

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")

    def fake_run(command, cwd=None, capture_output=None, text=None, timeout=None):
        observed["command"] = command
        observed["cwd"] = cwd
        observed["capture_output"] = capture_output
        observed["text"] = text
        observed["timeout"] = timeout
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(agent_builder_module.subprocess, "run", fake_run)

    assert builder.test_main_entrypoint(str(main_path), log_filename="runtime_test_log.txt") is True
    assert observed["command"] == ["/custom/python", str(main_path)]
    assert observed["cwd"] == str(tmp_path)
    assert observed["capture_output"] is True
    assert observed["text"] is True
    assert observed["timeout"] == 60
    assert isinstance(builder._runtime_service, RuntimeService)
    assert builder.log_path == str(tmp_path / "runtime_test_log.txt")
    log_text = Path(builder.log_path).read_text(encoding="utf-8")
    assert "Executing: /custom/python" in log_text
    assert "ok\n" in log_text
    assert "Test completed successfully" in log_text
    builder.close()


def test_node_modifier_reuses_tests_and_repairs_only_failed_nodes(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    _write_graph(
        graph_path,
        [
            {
                "name": "ExistingNode",
                "type": "WorkflowStepNode",
                "desc": "existing",
                "enable": True,
                "depends": [],
                "ext_data": {"type": "none", "desc": "none"},
            },
            {
                "name": "AddedNode",
                "type": "WorkflowStepNode",
                "desc": "added",
                "enable": True,
                "depends": ["ExistingNode"],
                "ext_data": {"type": "none", "desc": "none"},
            },
        ],
    )
    builder.graph_plan_path = str(graph_path)
    for node_name in ("ExistingNode", "AddedNode"):
        (tmp_path / f"{node_name}.py").write_text("# original\n", encoding="utf-8")
        test_path = tmp_path / "tests" / f"test_{node_name}.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text("def test_node(): pass\n", encoding="utf-8")
    cached_test = tmp_path / "tests" / "existing_custom.py"
    (tmp_path / "tests" / "test_ExistingNode.py").rename(cached_test)
    builder.dynamic_graph_cache["node_tests"] = {"ExistingNode": str(cached_test)}

    monkeypatch.setattr(agent_builder_module, "select_python_command", lambda: "/custom/python")
    observed_calls = []
    amended = []
    subprocess_results = iter(
        [
            SimpleNamespace(returncode=1, stdout="F\n", stderr="AssertionError: boom\n"),
            SimpleNamespace(returncode=0, stdout=".\n1 passed in 0.01s\n", stderr=""),
        ]
    )

    def fake_run(command, cwd=None, capture_output=None, text=None, timeout=None):
        observed_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return next(subprocess_results)

    monkeypatch.setattr(agent_builder_module.subprocess, "run", fake_run)

    class TestWriter(_FakeTestWriter):
        def write_test_from_node_file(self, *_args, **_kwargs):
            raise AssertionError("existing tests must be reused")

    class NodeWriter(_FakeModifierWriter):
        def amend_code_with_feedback(self, code_path, amendment, **_kwargs):
            amended.append(Path(code_path).name)
            assert "AssertionError: boom" in amendment
            Path(code_path).write_text("# repaired\n", encoding="utf-8")
            return Path(code_path)

    service = _replace_node_artifact_service(
        builder,
        node_test_writer=TestWriter(),
        node_writer=NodeWriter(),
        run_subprocess=fake_run,
    )
    workflow = service.workflow
    run_modifier = Mock(wraps=service.run)
    monkeypatch.setattr(service, "run", run_modifier)
    result = {
        name: builder.run_node_modifier(workflow_name="default", node_name=name, generate_test=False)
        for name in ("ExistingNode", "AddedNode")
    }

    assert observed_calls == [
        {
            "command": ["/custom/python", "-m", "pytest", str(cached_test), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
        {
            "command": ["/custom/python", "-m", "pytest", str(tmp_path / "tests" / "test_AddedNode.py"), "-q"],
            "cwd": str(tmp_path),
            "capture_output": True,
            "text": True,
            "timeout": 60,
        },
    ]
    assert list(result) == ["ExistingNode", "AddedNode"]
    requests = [call.args[0] for call in run_modifier.call_args_list]
    assert len(requests) == 2
    assert all(len(call.args) == 1 and not call.kwargs for call in run_modifier.call_args_list)
    assert len({id(request) for request in (*requests, *result.values())}) == 4
    for request, (name, context) in zip(requests, result.items()):
        _assert_fresh_modifier_request(request, context)
        assert request.node_name == context.node_name == name
        assert request.node_file_name == context.node_file_name == str(tmp_path / f"{name}.py")
        assert request.workflow_graph == context.workflow_graph == str(graph_path)
        assert request.working_directory == context.working_directory == str(tmp_path)
        assert request.skip_generate_node_test is True
        assert request.skip_run_node_test is False
        assert request.skip_amend_node is False
        assert context.log_file_path == str(tmp_path / "logs" / f"{name}_test.log")
    assert result["ExistingNode"].test_result["ok"] is False
    assert result["AddedNode"].test_result["ok"] is True
    assert result["ExistingNode"].test_file_path == str(cached_test)
    assert result["AddedNode"].test_file_path == str(tmp_path / "tests" / "test_AddedNode.py")
    assert result["ExistingNode"].amended_node_file_path == str(tmp_path / "ExistingNode.py")
    assert result["AddedNode"].amended_node_file_path == str(tmp_path / "AddedNode.py")
    assert amended == ["ExistingNode.py"]
    assert (tmp_path / "ExistingNode.py").read_text(encoding="utf-8") == "# repaired\n"
    assert (tmp_path / "AddedNode.py").read_text(encoding="utf-8") == "# original\n"
    assert "AssertionError: boom" in Path(result["ExistingNode"].log_file_path).read_text(encoding="utf-8")
    assert "1 passed in 0.01s" in Path(result["AddedNode"].log_file_path).read_text(encoding="utf-8")
    assert service.workflow is workflow
    _assert_no_request_cache(service)
    assert builder.dynamic_graph_cache["node_tests"] == {
        name: context.test_file_path for name, context in result.items()
    }
    assert builder.node_location_map == {
        name: str(tmp_path / f"{name}.py") for name in result
    }
    builder.close()