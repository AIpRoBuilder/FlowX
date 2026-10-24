"""Modified: verify exact class maps, scoped settings and isolated native pools."""
# Modified 2026-10-14: explicitly inject replacements while preserving cached pool snapshots.
# Modified 2026-10-14: exercise eager request-driven workflows and builder-owned published paths.
# Modified 2026-10-15: verify flat requests, shared batch artifacts and isolated node selections.
# Modified 2026-10-15: derive selected names from metadata and remove redundant routing/stage fields.
# Modified 2026-10-15: migrate to complete per-node requests and builder-local dependency snapshots.
# Modified 2026-10-15: remove batch expectations and verify request-local native planner skipping.
# Modified 2026-10-16: run NodeGenerationWorkflow directly now that the service wrapper is gone.

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import MISSING, FrozenInstanceError, dataclass, field, fields, replace
from pathlib import Path
from threading import Barrier
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from ag_ui_workflow import RunResult, StepRunOutput, WorkflowEngine, WorkflowStepNode

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.auditor.data import RuleViolation
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.tools import workflow_engine as workflow_engine_module
from flowx_core.worker.node_writer import (
    RequestNodeFileCoder, WorkflowStepNodeCoder,
)
from flowx_core.workflows import node_generation, node_runtime_modifier
from flowx_core.workflows.node_generation import NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import NodeRuntimeModifierRequest
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.architect.graph import NodeMeta


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@dataclass
class _FakePlanner(NodePlanElement):
    events: list[str] = field(default_factory=list)

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)

    def code_to_file(self, prompt, path, **kwargs):
        Path(path).write_text("# Node plan", encoding="utf-8")
        return Path(path)


@dataclass
class _FakeWriter(WorkflowStepNodeCoder):
    events: list[str] = field(default_factory=list)
    prompt_contexts: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        apply_node_settings(self)
        WorkflowStepNode.__init__(self)


@dataclass
class _FakeAuditor(NodeAuditor):
    audit: Callable[..., tuple[bool, list[RuleViolation]]] | None = None

    def __post_init__(self):
        super().__init__()

    def audit_file(self, file_path, node_meta=None, graph_plan_path=None):
        assert self.audit is not None, "the audit callable must be retained by scoped construction"
        return self.audit(file_path, node_meta, graph_plan_path=graph_plan_path)


def _make_builder(monkeypatch, root: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakePlanner)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    for name in ("GraphGenerationWorkflow", "MainEntrypointGenerationWorkflow", "NodeRuntimeModifierPipeline"):
        monkeypatch.setattr(agent_builder_module, name, Mock(name=name))
    return AgentBuilder(api_key="key", model="model", provider="openai", root_dir=str(root))


def _replace_node_workflow(builder: AgentBuilder) -> NodeGenerationWorkflow:
    """Explicitly replace an eager workflow after choosing new builder components."""
    old_workflow = builder._node_workflow
    old_pool = old_workflow.pool
    old_workflow.close()
    assert old_workflow.closed
    assert old_pool.closed
    workflow = NodeGenerationWorkflow(
        node_planner=builder.node_planner,
        node_coder=builder.node_coder,
        node_auditor=builder.node_auditor,
    )
    builder._node_workflow = workflow
    return workflow


def _capture_workflow_results(monkeypatch) -> list[RunResult]:
    """Observe native results without adding retained run state to production objects."""
    results: list[RunResult] = []
    real_run_steps = NodeGenerationWorkflow.run_steps

    def capture_result(self, *args, **kwargs):
        result = real_run_steps(self, *args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", capture_result)
    return results


def _spy_run_steps(monkeypatch) -> Mock:
    """Record request-level run_steps calls with the workflow as their first argument."""
    spy = Mock(name="run_steps")
    real_run_steps = NodeGenerationWorkflow.run_steps

    def capture(instance, *args, **kwargs):
        spy(instance, *args, **kwargs)
        return real_run_steps(instance, *args, **kwargs)

    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", capture)
    return spy


def _make_generation_request(root: Path, name: str = "First") -> node_generation.NodeGenerationRequest:
    raw_node = {
        "name": name, "type": "WorkflowStepNode", "desc": name.lower(),
        "planner_extra": {"labels": [name]},
    }
    return node_generation.NodeGenerationRequest(
        graph_plan_path=str(root / "workflow.json"), requirement_md_path=str(root / "requirement.md"),
        requirement_text=f"Generate {name}", node_meta=NodeMeta.from_dict(raw_node),
        raw_node=raw_node, target_path=root / f"{name}.py",
        markdown_path=root / "node_docs" / f"{name}.md",
    )


@pytest.fixture
def recording_node_workflow():
    """Record real native stages without replacing the engine or dispatching batches."""
    events = []
    native_states = []

    def observe(stage, node, session_state):
        assert node.get_workflow_param().state is session_state
        events.append(stage)
        native_states.append((
            stage, session_state, session_state["native_identity"],
            session_state["node_writer"]["node_meta"], session_state["auditor"]["node_meta"],
        ))

    class Planner(_FakePlanner):
        def process_input(self, user_input, dependency_results, session_state):
            observe("node_plan", self, session_state)
            return super().process_input(user_input, dependency_results, session_state)

        def plan_node(self, node, requirement_text, output_path, **kwargs):
            events.append("create_plan")
            Path(output_path).write_text("# generated plan", encoding="utf-8")
            return Path(output_path)

        def amend_node_plan(self, node, requirement_text, output_path, amendment, **kwargs):
            assert amendment == "Clarify the plan"
            assert Path(output_path).is_file()
            events.append("amend_plan")
            Path(output_path).write_text("# amended plan", encoding="utf-8")
            return Path(output_path)

    class Coder(_FakeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            observe("node_generate", self, session_state)
            assert set(dependency_results) == {"node_plan"}
            assert "previous_request" not in session_state
            session_state["previous_request"] = True
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            Path(output_path).write_text(f"# generated {name}\n", encoding="utf-8")
            return Path(output_path)

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("generation must not add an automatic repair or completeness retry")

    class Auditor(_FakeAuditor):
        def process_input(self, user_input, dependency_results, session_state):
            observe("node_audit", self, session_state)
            assert set(dependency_results) == {"node_generate"}
            return super().process_input(user_input, dependency_results, session_state)

    workflow = NodeGenerationWorkflow(
        node_planner=Planner(), node_coder=Coder(),
        node_auditor=Auditor(lambda *_args, **_kwargs: (True, [])),
    )
    engine = workflow
    try:
        yield workflow, events, native_states
    finally:
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed


def test_generation_and_modifier_requests_are_flat_keyword_only_models(tmp_path: Path) -> None:
    generation = _make_generation_request(tmp_path)
    repair = NodeRuntimeModifierRequest(
        node_file_name=str(tmp_path / "First.py"), workflow_graph=generation.graph_plan_path,
        working_directory=str(tmp_path),
    )
    for module, request, legacy_name, removed_fields in (
        (node_generation, generation, "NodeGenerationContext", {
            "node_name", "workflow_name", "node_locations", "skipped_node_names",
            "node_names", "selected_node_names", "node_docs_dir", "generate_markdowns",
            "markdown_amendments", "node_amendments", "workflow_requests", "workflow_registry",
            "node_formats", "raw_nodes", "artifacts",
        }),
        (node_runtime_modifier, repair, "NodeRuntimeModifierContext",
         {"workflow_name", "skipped_stages", "completed_stages"}),
    ):
        assert not hasattr(module, legacy_name)
        assert legacy_name not in module.__all__
        assert type(request).__name__ in module.__all__
        assert "context" not in {setting.name for setting in fields(request)}
        assert not hasattr(request, "context")
        assert removed_fields.isdisjoint(setting.name for setting in fields(request))
        assert all(not hasattr(request, name) for name in removed_fields)
        assert all(setting.kw_only for setting in fields(request) if setting.init)
    assert node_generation.NodeGenerationRequest.__dataclass_params__.frozen is True
    assert NodeRuntimeModifierRequest.__dataclass_params__.frozen is False
    assert not hasattr(AgentBuilder, "_generate_node_context")
    assert not hasattr(AgentBuilder, "_generate_node_request")
    required = {
        "graph_plan_path", "requirement_md_path", "requirement_text", "node_meta",
        "raw_node", "target_path", "markdown_path",
    }
    optional = {
        "language", "temperature", "generate_markdown", "skip_node_planner", "markdown_amendment", "node_amendment",
        "node_format", "dependency_context", "state",
    }
    assert {setting.name for setting in fields(generation)} == required | optional
    assert {
        setting.name for setting in fields(generation)
        if setting.default is MISSING and setting.default_factory is MISSING
    } == required
    assert all(setting.init for setting in fields(generation))
    assert type(generation.node_meta) is NodeMeta
    assert generation.node_meta.name == generation.raw_node["name"] == "First"
    assert generation.target_path == tmp_path / "First.py"
    assert generation.markdown_path == tmp_path / "node_docs" / "First.md"
    assert (generation.language, generation.temperature, generation.generate_markdown) == ("python", 0.35, True)
    assert generation.skip_node_planner is False
    assert next(setting for setting in fields(generation) if setting.name == "skip_node_planner").default is False
    assert generation.markdown_amendment == generation.node_amendment == generation.dependency_context == ""
    assert generation.node_format == generation.state == {}
    assert type(generation.node_format) is type(generation.state) is dict
    with pytest.raises(FrozenInstanceError):
        generation.language = "javascript"
    with pytest.raises(TypeError):
        node_generation.NodeGenerationRequest(generation.graph_plan_path)
    assert {setting.name for setting in fields(repair) if not setting.init} == {
        "test_file_path", "log_file_path", "test_log", "test_result", "amended_node_file_path",
    }


def test_builder_batch_helpers_are_absent_and_public_generation_is_disabled() -> None:
    for name in (
        "_generate_nodes", "_normalize_requested_names", "_node_path_for",
        "_render_dependency_context", "_read_node_artifact", "_supports_backend_node_tests",
        "_start_progress", "_advance_progress", "_print_progress_bar",
    ):
        assert not hasattr(AgentBuilder, name)
    # No constructor state or valid paths are needed: the boundary must fail immediately.
    builder = object.__new__(AgentBuilder)
    with pytest.raises(NotImplementedError) as error:
        builder.generate_nodes()
    assert str(error.value) == (
        "Batch node generation is not implemented; use build_nodes() to generate a workflow's nodes."
    )
    assert vars(builder) == {}


@pytest.mark.parametrize("missing", [
    "graph_plan_path", "requirement_md_path", "requirement_text", "node_meta",
    "raw_node", "target_path", "markdown_path",
])
def test_generation_request_requires_complete_constructor_inputs(tmp_path: Path, missing: str) -> None:
    request = _make_generation_request(tmp_path)
    inputs = {setting.name: getattr(request, setting.name) for setting in fields(request)}
    del inputs[missing]
    with pytest.raises(TypeError, match=missing):
        node_generation.NodeGenerationRequest(**inputs)


def test_modifier_request_replacement_preserves_inputs_and_resets_outputs(tmp_path: Path) -> None:
    request = NodeRuntimeModifierRequest(
        node_file_name="First.py", workflow_graph={"nodes": [{"name": "First"}]},
        working_directory=str(tmp_path), state={"native_identity": object()},
    )
    request.test_file_path = "old_test.py"
    request.log_file_path = "old_test.log"
    request.test_log = "old failure"
    request.test_result = {"ok": False}
    request.amended_node_file_path = "old_First.py"
    first, second = replace(request), replace(request)

    assert len({id(item) for item in (request, first, second)}) == 3
    for result in (first, second):
        for setting in fields(request):
            if setting.init:
                assert getattr(result, setting.name) is getattr(request, setting.name)
        assert result.test_file_path == result.log_file_path == result.test_log == ""
        assert result.test_result is None
        assert result.amended_node_file_path == ""
    first.test_file_path = "new_test.py"
    first.test_result = {"ok": True}
    second.test_result = {"ok": False}
    assert len({id(item.test_result) for item in (request, first, second)}) == 3
    first.test_result["returncode"] = 0
    assert first.test_result == {"ok": True, "returncode": 0}
    assert second.test_file_path == ""
    assert second.test_result == {"ok": False}
    assert request.test_file_path == "old_test.py"
    assert request.log_file_path == "old_test.log"
    assert request.test_log == "old failure"
    assert request.test_result == {"ok": False}
    assert request.amended_node_file_path == "old_First.py"


@pytest.mark.parametrize("output_name", [
    "test_file_path", "log_file_path", "test_log", "test_result", "amended_node_file_path",
])
def test_modifier_request_outputs_reject_constructor_and_replace_keywords(tmp_path: Path, output_name: str) -> None:
    inputs = {
        "node_file_name": "First.py", "workflow_graph": {"nodes": [{"name": "First"}]},
        "working_directory": str(tmp_path),
    }
    request = NodeRuntimeModifierRequest(**inputs)
    output = {"ok": False} if output_name == "test_result" else "stale output"
    with pytest.raises(TypeError, match=output_name):
        NodeRuntimeModifierRequest(**inputs, **{output_name: output})
    with pytest.raises(ValueError, match=output_name):
        replace(request, **{output_name: output})
    assert request.test_file_path == request.log_file_path == request.test_log == ""
    assert request.test_result is None
    assert request.amended_node_file_path == ""


def test_generation_requests_have_independent_defaults_and_shallow_replacements(tmp_path: Path) -> None:
    first = _make_generation_request(tmp_path)
    second = _make_generation_request(tmp_path, "Second")
    independent = _make_generation_request(tmp_path)

    assert len({id(request) for request in (first, second, independent)}) == 3
    assert type(first.node_meta) is type(second.node_meta) is NodeMeta
    assert first.node_meta is not second.node_meta
    assert (first.node_meta.name, first.target_path) == ("First", tmp_path / "First.py")
    assert (second.node_meta.name, second.target_path) == ("Second", tmp_path / "Second.py")
    for name in ("raw_node", "node_format", "state"):
        assert len({id(getattr(request, name)) for request in (first, second, independent)}) == 3
    first.node_format["user_input_format"] = {"query": "string"}
    first.raw_node["first_only"] = True
    first.state["native_identity"] = object()
    for request in (second, independent):
        assert request.node_format == request.state == {}
        assert "first_only" not in request.raw_node

    amended = replace(
        first, markdown_amendment="Clarify the plan", node_amendment="Keep the contract",
        dependency_context="Previously generated file:\n# prior node", skip_node_planner=True,
    )
    assert amended is not first
    for setting in fields(first):
        if setting.name not in {"markdown_amendment", "node_amendment", "dependency_context", "skip_node_planner"}:
            assert getattr(amended, setting.name) is getattr(first, setting.name)
    assert amended.state["native_identity"] is first.state["native_identity"]
    assert amended.markdown_amendment == "Clarify the plan"
    assert amended.node_amendment == "Keep the contract"
    assert amended.skip_node_planner is True
    assert first.skip_node_planner is second.skip_node_planner is independent.skip_node_planner is False
    assert first.markdown_amendment == first.node_amendment == first.dependency_context == ""


def test_generation_context_renders_one_node_and_preformatted_dependencies(tmp_path: Path) -> None:
    request = replace(
        _make_generation_request(tmp_path),
        node_format=MappingProxyType({"user_input_format": {"query": "string"}}),
        dependency_context="### Prior\nMarkdown:\n# Prior plan\nGenerated file:\n# generated Prior",
    )
    context = request.render_generation_context()
    assert context.startswith("Node generation request context (authoritative):\n- node: First\n")
    assert request.requirement_text in context
    assert '"query": "string"' in context
    assert context.endswith(request.dependency_context)
    with pytest.raises(TypeError):
        request.render_generation_context("First")


def test_plan_request_shallow_copies_raw_mapping_without_losing_extras(tmp_path: Path) -> None:
    request = _make_generation_request(tmp_path)
    raw_node = MappingProxyType(dict(request.raw_node))
    request = replace(request, raw_node=raw_node)
    plan = node_generation.NodeGenerationWorkflow.build_plan_request(request)
    assert type(plan["node"]) is dict
    assert plan["node"] == raw_node
    assert plan["node"] is not raw_node
    assert plan["node"]["planner_extra"] is raw_node["planner_extra"]
    assert plan["requirement_text"] == request.requirement_text
    assert plan["output_path"] == str(request.markdown_path)
    assert plan["generate_markdown"] is True
    assert plan["amendment"] == ""
    assert plan["temperature"] == 0.2
    plan["node"]["planner_only"] = True
    assert "planner_only" not in raw_node


@pytest.mark.parametrize("missing", ["node_meta", "target_path"])
def test_generation_rejects_incomplete_requests_before_engine_run(monkeypatch, tmp_path: Path, missing: str) -> None:
    complete = _make_generation_request(tmp_path)
    request = replace(complete, **{missing: None})
    run = Mock(name="run_steps")
    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", run)
    # Input validation precedes native execution; it needs no extra pool lifetime.
    workflow = object.__new__(node_generation.NodeGenerationWorkflow)
    workflow._closed = False
    with pytest.raises(ValueError, match="requires named node_meta and target_path"):
        workflow.run(request)
    run.assert_not_called()
    assert complete.node_meta.name == "First"
    assert complete.target_path == tmp_path / "First.py"
    assert request.state is complete.state


def test_generation_rejects_empty_metadata_name_before_engine_run(monkeypatch, tmp_path: Path) -> None:
    complete = _make_generation_request(tmp_path)
    request = replace(complete, node_meta=replace(complete.node_meta, name=""))
    run = Mock(name="run_steps")
    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", run)
    workflow = object.__new__(node_generation.NodeGenerationWorkflow)
    workflow._closed = False
    with pytest.raises(ValueError, match="requires named node_meta and target_path"):
        workflow.run(request)
    run.assert_not_called()
    assert type(request.node_meta) is NodeMeta
    assert request.node_meta.name == ""
    assert complete.node_meta.name == "First"
    assert request.state is complete.state


@pytest.mark.parametrize("raw_node", [{"name": "Second"}, {"name": ""}, {}])
def test_generation_rejects_raw_node_name_mismatch_before_engine_run(monkeypatch, tmp_path: Path, raw_node) -> None:
    request = replace(_make_generation_request(tmp_path), raw_node=raw_node)
    run = Mock(name="run_steps")
    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", run)
    workflow = object.__new__(node_generation.NodeGenerationWorkflow)
    workflow._closed = False
    with pytest.raises(ValueError, match=r"raw_node\.name must match node_meta\.name"):
        workflow.run(request)
    run.assert_not_called()
    assert type(request.node_meta) is NodeMeta
    assert request.node_meta.name == "First"
    assert request.raw_node.get("name") != request.node_meta.name
    assert request.state == {}


def test_generation_rejects_missing_markdown_path_before_engine_run(monkeypatch, tmp_path: Path) -> None:
    request = replace(_make_generation_request(tmp_path), markdown_path=None, generate_markdown=False)
    run = Mock(name="run_steps")
    monkeypatch.setattr(NodeGenerationWorkflow, "run_steps", run)
    workflow = object.__new__(node_generation.NodeGenerationWorkflow)
    workflow._closed = False
    with pytest.raises(ValueError, match="requires markdown_path"):
        workflow.run(request)
    run.assert_not_called()
    assert request.state == {}


def test_node_generation_workflow_runs_single_eager_workflow_from_request(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    results = _capture_workflow_results(monkeypatch)
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Standalone", "type": "WorkflowStepNode", "desc": "standalone",
         "depends": [], "ext_data": {"type": "none"}},
    ]}), encoding="utf-8")
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Build standalone node", encoding="utf-8")

    class Planner(NodePlanElement):
        def __post_init__(self):
            apply_node_settings(self)
            WorkflowStepNode.__init__(self)

        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("# Standalone plan", encoding="utf-8")
            return Path(path)

    class Coder(_FakeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            assert self.get_workflow_param().state is session_state
            assert session_state is not request.state
            assert session_state["native_identity"] is request.state["native_identity"]
            assert session_state["node_writer"]["node_meta"] is request.node_meta
            assert session_state["auditor"]["node_meta"] is request.node_meta
            return super().process_input(user_input, dependency_results, session_state)

        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("class Standalone: pass\n", encoding="utf-8")
            return Path(path)

    def audit(_path, meta, **_kwargs):
        assert meta is request.node_meta
        return True, []

    builder.graph_plan_path = str(graph_path)
    builder.requirement_md_path = str(requirement_path)
    builder.node_planner = Planner()
    builder.node_coder = Coder()
    builder.node_auditor = _FakeAuditor(audit)
    workflow = _replace_node_workflow(builder)
    engine = workflow
    try:
        assert type(engine) is NodeGenerationWorkflow
        assert isinstance(engine, WorkflowEngine)
        assert not any(
            isinstance(value, node_generation.NodeGenerationRequest) for value in vars(workflow).values()
        )
        docs = tmp_path / "node_docs"
        docs.mkdir()
        node = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"][0]
        native_identity = object()
        request = node_generation.NodeGenerationRequest(
            graph_plan_path=str(graph_path), requirement_md_path=str(requirement_path),
            requirement_text="Build standalone node", node_meta=NodeMeta.from_dict(node),
            raw_node=node, target_path=tmp_path / "Standalone.py", markdown_path=docs / "Standalone.md",
            state={"native_identity": native_identity},
        )
        generated_path = workflow.run(request)

        assert type(generated_path) is str
        assert generated_path == str(request.target_path)
        assert request.state == {"native_identity": native_identity}
        assert type(request.node_meta) is NodeMeta
        assert request.node_meta.name == "Standalone"
        assert request.markdown_path.read_text(encoding="utf-8") == "# Standalone plan"
        assert Path(generated_path).read_text(encoding="utf-8") == "class Standalone: pass\n"
        assert not hasattr(request, "artifacts")  # Publishing belongs to the builder, not the workflow.
        assert builder.node_location_map == {}
        assert builder.artifact_state.workflow_artifacts == {}
        assert len(results) == 1
        assert results[0].outputs["node_audit"].derived["ok"] is True
        assert workflow is engine
        assert not hasattr(workflow, "_last_result")
    finally:
        builder.close()
    assert workflow.closed
    assert engine.pool.closed


def test_node_generation_has_three_ordered_stages_and_applies_explicit_amendment(
    monkeypatch, tmp_path: Path
) -> None:
    results = _capture_workflow_results(monkeypatch)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate one step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "First", "type": "WorkflowStepNode", "desc": "first", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
        {"name": "Second", "type": "WorkflowStepNode", "desc": "second", "enable": True,
         "depends": ["First"], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    events: list[str] = []
    configs = []
    real_build_pool = workflow_engine_module.build_workflow_pool

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_build_pool(node_classes, workflow_config)

    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", capture_config)
    (tmp_path / "Second.py").write_text("# existing Second", encoding="utf-8")
    docs = tmp_path / "node_docs"
    docs.mkdir()
    (docs / "Second.md").write_text("# existing Second plan", encoding="utf-8")

    class FakePlanner(_FakePlanner):
        def plan_node(self, node, requirement_text, output_path, **kwargs):
            name = node["name"]
            assert name == "First"
            assert requirement_text == "Generate one step"
            self.events.append(f"{name}:plan")
            path = Path(output_path)
            path.write_text(f"# {name} plan", encoding="utf-8")
            return path

        def amend_node_plan(self, node, requirement_text, output_path, amendment, **kwargs):
            assert node["name"] == "Second"
            assert requirement_text == "Generate one step"
            assert amendment == "Clarify the second plan"
            path = Path(output_path)
            assert path.read_text(encoding="utf-8") == "# existing Second plan"
            self.events.append("Second:plan")
            path.write_text("# amended Second plan", encoding="utf-8")
            return path

    class FakeCoder(_FakeWriter):
        def write_node_from_requirement(self, name, meta, _requirement, output_path, **kwargs):
            assert name == "First"
            assert meta is first.node_meta
            assert "Requested amendment (apply while generating):\nKeep first contract" in kwargs["additional_generation_context"]
            self.events.append(f"{name}:write")
            path = Path(output_path)
            path.write_text(f"# initial {name}", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, file_path, feedback, **kwargs):
            self.events.append("Second:repair")
            assert feedback == "Apply requested fix"
            assert second.node_amendment == feedback
            assert "# initial First" in kwargs["additional_generation_context"]
            assert "# First plan" in kwargs["additional_generation_context"]
            Path(file_path).write_text("# repaired Second", encoding="utf-8")
            return Path(file_path)

    def audit(file_path, meta, **kwargs):
        name = Path(file_path).stem
        assert meta is (first if name == "First" else second).node_meta
        events.append(f"{name}:audit")
        if name == "Second" and "repaired" not in Path(file_path).read_text(encoding="utf-8"):
            return False, [RuleViolation(class_name=name, rule="bad_code", detail="repair needed", lineno=1)]
        return True, []

    workflow = NodeGenerationWorkflow(
        node_planner=FakePlanner(events=events), node_coder=FakeCoder(events=events),
        node_auditor=_FakeAuditor(audit),
    )
    engine = workflow
    try:
        nodes = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
        first = node_generation.NodeGenerationRequest(
            graph_plan_path=str(graph_path), requirement_md_path=str(requirement_path),
            requirement_text="Generate one step", node_meta=NodeMeta.from_dict(nodes[0]),
            raw_node=nodes[0], target_path=tmp_path / "First.py", markdown_path=docs / "First.md",
            node_amendment="Keep first contract",
        )
        first_path = workflow.run(first)
        second = node_generation.NodeGenerationRequest(
            graph_plan_path=str(graph_path), requirement_md_path=str(requirement_path),
            requirement_text="Generate one step", node_meta=NodeMeta.from_dict(nodes[1]),
            raw_node=nodes[1], target_path=tmp_path / "Second.py", markdown_path=docs / "Second.md",
            markdown_amendment="Clarify the second plan", node_amendment="Apply requested fix",
            dependency_context="### First\nMarkdown:\n# First plan\nGenerated file:\n# initial First",
        )
        second_path = workflow.run(second)

        assert first_path == str(first.target_path)
        assert second_path == str(second.target_path)
        assert first.generate_markdown is second.generate_markdown is True
        assert first.skip_node_planner is second.skip_node_planner is False
        assert len(configs) == 1  # Every node request uses the same eagerly initialized pool.
        component_classes = {"node_plan": FakePlanner, "node_generate": FakeCoder, "node_audit": _FakeAuditor}
        for node_classes, config in configs:
            assert node_classes == component_classes
            assert list(node_classes) == list(component_classes)
            assert all(node_classes[name] is cls for name, cls in component_classes.items())
            assert workflow._node_classes is node_classes
            assert engine.steps_meta == [cls.step_meta() for cls in component_classes.values()]
            assert config["pool_size"] == 1
            assert {node["name"]: node.get("depends", []) for node in config["nodes"]} == {
                "node_plan": [], "node_generate": ["node_plan"], "node_audit": ["node_generate"],
            }
        assert events == [
            "First:plan", "First:write", "First:audit",
            "Second:plan", "Second:repair", "Second:audit",
        ]
        assert Path(second_path).read_text(encoding="utf-8") == "# repaired Second"
        assert second.markdown_path.read_text(encoding="utf-8") == "# amended Second plan"
        assert len(results) == 2
        assert all(set(result.outputs) == {"node_plan", "node_generate", "node_audit"} for result in results)
        assert all(result.outputs["node_audit"].derived["ok"] is True for result in results)
        assert workflow._node_classes == component_classes
        assert workflow is engine
        assert not hasattr(workflow, "_last_result")
    finally:
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed


@pytest.mark.parametrize("skip_node_planner", [False, True])
def test_failed_audit_raises_without_repair_or_retry(monkeypatch, tmp_path: Path, skip_node_planner) -> None:
    results = _capture_workflow_results(monkeypatch)
    request = replace(
        _make_generation_request(tmp_path, "Broken"), generate_markdown=False,
        skip_node_planner=skip_node_planner, state={"native_identity": object()},
    )
    events = []
    native_states = []

    class Planner(_FakePlanner):
        def process_input(self, *_args, **_kwargs):
            raise AssertionError("the native planner must be skipped")

    class FakeCoder(_FakeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            assert self.get_workflow_param().state is session_state
            assert session_state is not request.state
            assert session_state["native_identity"] is request.state["native_identity"]
            assert session_state["node_writer"]["node_meta"] is request.node_meta
            assert session_state["auditor"]["node_meta"] is request.node_meta
            native_states.append(session_state)
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            events.append("write")
            path = Path(output_path)
            path.write_text("# invalid", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("failed audits must not trigger automatic repairs")

    def audit(_path, meta, **_kwargs):
        events.append("audit")
        assert meta is request.node_meta
        return False, [RuleViolation(class_name="Broken", rule="bad_code", detail="invalid", lineno=1)]

    class Auditor(_FakeAuditor):
        def process_input(self, user_input, dependency_results, session_state):
            assert self.get_workflow_param().state is session_state is native_states[0]
            return super().process_input(user_input, dependency_results, session_state)

    workflow = NodeGenerationWorkflow(node_planner=Planner(), node_coder=FakeCoder(), node_auditor=Auditor(audit))
    try:
        with pytest.raises(RuntimeError, match="bad_code"):
            workflow.run(request)

        assert request.node_meta.name == request.raw_node["name"] == "Broken"
        assert request.target_path.read_text(encoding="utf-8") == "# invalid"
        assert not request.markdown_path.exists()
        assert not hasattr(request, "artifacts")
        assert events == ["write", "audit"]
        assert len(native_states) == 1
        assert set(request.state) == {"native_identity"}
        assert len(results) == 1
        assert set(results[0].outputs) == {"node_plan", "node_generate", "node_audit"}
        assert results[0].outputs["node_audit"].derived["ok"] is False
        assert results[0].outputs["node_plan"].card == {"status": "skipped"}
        assert not any(
            isinstance(value, node_generation.NodeGenerationRequest) for value in vars(workflow).values()
        )
        assert not hasattr(workflow, "_last_result")
    finally:
        workflow.close()


def test_generation_reuses_pool_and_constructor_snapshots_when_components_change(monkeypatch, tmp_path) -> None:
    results = _capture_workflow_results(monkeypatch)
    graph = tmp_path / "workflow.json"
    graph.write_text(json.dumps({"nodes": [{"name": "Example", "type": "WorkflowStepNode"}]}), encoding="utf-8")
    requirement = tmp_path / "requirement.md"
    requirement.write_text("One node", encoding="utf-8")
    events = []

    class Planner(_FakePlanner):
        def process_input(self, user_input, dependency_results, session_state):
            self.events.append("plan")
            output_path = session_state["node_plan_request"]["output_path"]
            Path(output_path).write_text("Node plan", encoding="utf-8")
            return StepRunOutput(derived={"markdown_path": output_path})

    class Coder(_FakeWriter):
        def process_input(self, user_input, dependency_results, session_state):
            assert "old_run" not in session_state
            session_state["old_run"] = True
            self.events.append("write")
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            Path(output_path).write_text("# generated", encoding="utf-8")
            return Path(output_path)

    audits = []
    original_auditor = _FakeAuditor(lambda *_a, **_kw: audits.append("original") or (True, []))
    workflow = NodeGenerationWorkflow(
        node_planner=Planner(events=events), node_coder=Coder(events=events), node_auditor=original_auditor,
    )
    engine = workflow
    try:
        docs = tmp_path / "node_docs"
        docs.mkdir()
        request = replace(
            _make_generation_request(tmp_path, "Example"), requirement_text="One node",
            generate_markdown=False, state={"native_identity": object()},
        )
        assert workflow.run(request) == str(request.target_path)
        assert results[-1].outputs["node_plan"].card == {"status": "skipped"}
        first_result = results[-1]
        planned = replace(request, generate_markdown=True)
        assert workflow.run(planned) == str(planned.target_path)
        assert workflow is engine
        assert results[-1].request_id != first_result.request_id
        assert events == ["write", "plan", "write"]
        replacement_auditor = _FakeAuditor(lambda *_a, **_kw: pytest.fail("replacement was not constructed into the pool"))
        workflow.node_auditor = replacement_auditor
        workflow.node_coder.write_node_from_requirement = lambda *_a, **_kw: pytest.fail("instance monkeypatch")

        class ReplacementCoder(_FakeWriter):
            def process_input(self, *_args, **_kwargs):
                raise AssertionError("replacement coder was not constructed into the pool")

        workflow.node_coder = ReplacementCoder()
        # Prototype mutations must not lazily rebuild or reconfigure the native pool.
        assert workflow.run(planned) == str(planned.target_path)
        assert results[-1].outputs["node_plan"].card == {"status": "skipped"}
        assert workflow.node_auditor is replacement_auditor
        assert events == ["write", "plan", "write", "write"]
        assert audits == ["original", "original", "original"]
        assert workflow is engine
        assert workflow._node_classes == {"node_plan": Planner, "node_generate": Coder, "node_audit": _FakeAuditor}
        assert len({result.request_id for result in results}) == 3
        assert request.state is planned.state
        assert set(request.state) == {"native_identity"}
        assert not hasattr(workflow, "_last_result")
    finally:
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed


def test_generation_does_not_retry_completeness_after_a_successful_audit(monkeypatch, tmp_path: Path) -> None:
    results = _capture_workflow_results(monkeypatch)
    request = replace(
        _make_generation_request(tmp_path), skip_node_planner=True,
        markdown_amendment="Clarify the plan",
    )
    events = []

    class Planner(_FakePlanner):
        def process_input(self, *_args, **_kwargs):
            raise AssertionError("missing markdown must not override the explicit planner skip")

    class Coder(_FakeWriter):
        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            events.append("write")
            assert meta is request.node_meta
            # The auditor owns acceptance; the workflow must not add a completeness sweep.
            return Path(output_path)

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("a successful audit must not trigger a completeness repair")

    def audit(file_path, meta, **_kwargs):
        events.append("audit")
        assert meta is request.node_meta
        assert not Path(file_path).exists()
        return True, []

    workflow = NodeGenerationWorkflow(node_planner=Planner(), node_coder=Coder(), node_auditor=_FakeAuditor(audit))
    try:
        assert workflow.run(request) == str(request.target_path)
        assert events == ["write", "audit"]
        assert request.generate_markdown is True
        assert not request.target_path.exists()
        assert not request.markdown_path.exists()
        assert len(results) == 1
        assert set(results[0].outputs) == {"node_plan", "node_generate", "node_audit"}
        assert results[0].outputs["node_plan"].card == {"status": "skipped"}
        assert results[0].outputs["node_audit"].derived["ok"] is True
        assert not hasattr(workflow, "_last_result")
    finally:
        workflow.close()


@pytest.mark.parametrize("skip_node_planner", [False, True], ids=["automatic", "explicit-skip"])
@pytest.mark.parametrize("generate_markdown", [False, True], ids=["no-generation", "generate"])
@pytest.mark.parametrize("markdown_amendment", ["", "Clarify the plan"], ids=["no-amendment", "amend"])
@pytest.mark.parametrize("existing_markdown", [False, True], ids=["missing-md", "existing-md"])
def test_generation_native_planner_skip_matrix(
    monkeypatch, tmp_path: Path, recording_node_workflow,
    skip_node_planner, generate_markdown, markdown_amendment, existing_markdown,
) -> None:
    workflow, events, native_states = recording_node_workflow
    results = _capture_workflow_results(monkeypatch)
    run = _spy_run_steps(monkeypatch)
    engine = workflow
    request = replace(
        _make_generation_request(tmp_path), skip_node_planner=skip_node_planner,
        generate_markdown=generate_markdown, markdown_amendment=markdown_amendment,
        state={"native_identity": object()},
    )
    request.markdown_path.parent.mkdir()
    original_markdown = "# existing plan\n"
    if existing_markdown:
        request.markdown_path.write_text(original_markdown, encoding="utf-8")
    expected_skip = skip_node_planner or not (
        (generate_markdown and not existing_markdown) or markdown_amendment
    )

    generated = workflow.run(request)

    run.assert_called_once()
    assert run.call_args.args == (engine,)
    assert run.call_args.kwargs["skipped_steps"] == (("node_plan",) if expected_skip else ())
    assert generated == str(request.target_path)
    assert request.target_path.read_text(encoding="utf-8") == "# generated First\n"
    expected_events = []
    if not expected_skip:
        expected_events.append("node_plan")
        if not existing_markdown:
            expected_events.append("create_plan")
        if markdown_amendment:
            expected_events.append("amend_plan")
    assert events == expected_events + ["node_generate", "node_audit"]
    assert len(results) == 1
    result = results[0]
    assert set(result.outputs) == {"node_plan", "node_generate", "node_audit"}
    assert result.outputs["node_generate"].derived["generated_path"] == generated
    assert result.outputs["node_audit"].derived["ok"] is True
    if expected_skip:
        assert result.outputs["node_plan"].card == {"status": "skipped"}
        if existing_markdown:
            assert request.markdown_path.read_text(encoding="utf-8") == original_markdown
        else:
            assert not request.markdown_path.exists()
    else:
        assert result.outputs["node_plan"].card != {"status": "skipped"}
        assert result.outputs["node_plan"].derived["markdown_path"] == str(request.markdown_path.resolve())
        assert request.markdown_path.read_text(encoding="utf-8") == (
            "# amended plan" if markdown_amendment else "# generated plan"
        )
    assert [stage for stage, *_ in native_states] == (
        (["node_plan"] if not expected_skip else []) + ["node_generate", "node_audit"]
    )
    for _, state, native_identity, writer_meta, auditor_meta in native_states:
        assert state is native_states[0][1]
        assert state is not request.state
        assert native_identity is request.state["native_identity"]
        assert writer_meta is auditor_meta is request.node_meta
    assert set(request.state) == {"native_identity"}
    assert request.skip_node_planner is skip_node_planner
    assert request.generate_markdown is generate_markdown
    assert request.markdown_amendment == markdown_amendment
    assert workflow is engine
    assert not hasattr(workflow, "_last_result")


@pytest.mark.parametrize("generate_markdown", [False, True])
def test_generation_reused_workflow_honors_each_planner_skip_without_leakage(
    monkeypatch, tmp_path: Path, recording_node_workflow, generate_markdown,
) -> None:
    workflow, events, native_states = recording_node_workflow
    results = _capture_workflow_results(monkeypatch)
    run = _spy_run_steps(monkeypatch)
    engine = workflow
    base = replace(
        _make_generation_request(tmp_path), generate_markdown=generate_markdown,
        markdown_amendment="Clarify the plan",
    )
    base.markdown_path.parent.mkdir()
    observed_requests = []
    for index, skip in enumerate((True, False, True)):
        # A missing brief with an amendment always needs the planner unless explicitly skipped.
        request = replace(
            base, skip_node_planner=skip, state={"native_identity": object()},
        )
        observed_requests.append(request)
        assert not request.markdown_path.exists()
        events.clear()
        native_states.clear()

        assert workflow.run(request) == str(request.target_path)

        assert run.call_count == index + 1
        assert run.call_args.args == (engine,)
        assert run.call_args.kwargs["skipped_steps"] == (("node_plan",) if skip else ())
        assert len(results) == index + 1
        result = results[-1]
        assert set(result.outputs) == {"node_plan", "node_generate", "node_audit"}
        assert result.outputs["node_audit"].derived["ok"] is True
        if skip:
            assert events == ["node_generate", "node_audit"]
            assert result.outputs["node_plan"].card == {"status": "skipped"}
            assert not request.markdown_path.exists()
        else:
            assert events == ["node_plan", "create_plan", "amend_plan", "node_generate", "node_audit"]
            assert result.outputs["node_plan"].card != {"status": "skipped"}
            assert request.markdown_path.read_text(encoding="utf-8") == "# amended plan"
            request.markdown_path.unlink()
        for _, state, identity, writer_meta, auditor_meta in native_states:
            assert state is native_states[0][1]
            assert state is not request.state
            assert identity is request.state["native_identity"]
            assert writer_meta is auditor_meta is request.node_meta
        assert set(request.state) == {"native_identity"}
        assert workflow is engine
    assert len({result.request_id for result in results}) == 3
    assert [request.skip_node_planner for request in observed_requests] == [True, False, True]
    assert len({id(request.state) for request in observed_requests}) == 3
    assert not hasattr(workflow, "_last_result")


@pytest.mark.parametrize("pool_size", [1, 2])
def test_generation_constructs_real_nodes_and_isolates_reused_concurrent_slots(monkeypatch, tmp_path, pool_size):
    planners, coders, auditors, requests, configs = [], [], [], [], []
    planner_states = []
    native_states = []
    results = _capture_workflow_results(monkeypatch)
    barrier = Barrier(2) if pool_size == 2 else None

    @dataclass
    class Planner(_FakePlanner):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Planner
            assert self.STEP_ID == "node_plan"
            assert self.DEPENDENCIES == []
            assert self.get_workflow_param().state is session_state
            planner_states.append((
                session_state, session_state["request_label"], session_state["node_plan_request"]["node"],
            ))
            return super().process_input(user_input, dependency_results, session_state)

    @dataclass
    class Coder(_FakeWriter):
        instances: list = field(default_factory=list)
        requests: list = field(default_factory=list)
        barrier: object = None

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Coder
            assert self.STEP_ID == "node_generate"
            assert self.DEPENDENCIES == ["node_plan"]
            assert set(dependency_results) == {"node_plan"}
            assert self.get_workflow_param().state is session_state
            assert session_state["node_writer"]["node_name"] == session_state["node_writer"]["node_meta"].name
            # Capture identities during the lease, not mutable state after pool cleanup.
            native_states.append((
                session_state, session_state["request_label"], session_state["native_identity"],
                session_state["node_writer"]["node_meta"], session_state["auditor"]["node_meta"],
            ))
            assert "previous_request" not in session_state
            assert session_state["request_label"] == Path(session_state["node_writer"]["requirement_md_path"]).parent.name
            session_state["previous_request"] = True
            return super().process_input(user_input, dependency_results, session_state)

        def write_node_from_requirement(self, name, meta, requirement_md_path, output_path, **kwargs):
            assert type(meta) is NodeMeta
            assert name == meta.name
            assert self.get_workflow_param().state["node_writer"]["node_meta"] is meta
            label = Path(requirement_md_path).parent.name
            assert f"# prior {label}" in kwargs["additional_generation_context"]
            if self.barrier is not None and Path(requirement_md_path).parent.name != "third":
                self.barrier.wait(timeout=10)
            self.requests.append((self, output_path, kwargs["temperature"], kwargs["language"]))
            Path(output_path).write_text(f"# generated {Path(requirement_md_path).parent.name}", encoding="utf-8")
            return Path(output_path)

    @dataclass
    class Auditor(_FakeAuditor):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

        def process_input(self, user_input, dependency_results, session_state):
            assert type(self) is Auditor
            assert self.STEP_ID == "node_audit"
            assert self.DEPENDENCIES == ["node_generate"]
            assert set(dependency_results) == {"node_generate"}
            assert self.get_workflow_param().state is session_state
            assert type(session_state["auditor"]["node_meta"]) is NodeMeta
            assert session_state["auditor"]["node_meta"] is session_state["node_writer"]["node_meta"]
            return super().process_input(user_input, dependency_results, session_state)

    planner = Planner(instances=planners)
    coder = Coder(instances=coders, requests=requests, barrier=barrier)
    auditor = Auditor(lambda *_args, **_kwargs: (True, []), auditors)
    planners.clear()
    coders.clear()
    auditors.clear()
    original_metadata = [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Planner, Coder, Auditor)]
    config = node_generation.NodeGenerationWorkflow.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(node_generation.NodeGenerationWorkflow, "json_config", staticmethod(lambda: config))
    real_build_pool = workflow_engine_module.build_workflow_pool

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_build_pool(node_classes, workflow_config)

    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", capture_config)

    def request(label, temperature, language="python", generate_markdown=True):
        root = tmp_path / label
        root.mkdir()
        docs = root / "node_docs"
        docs.mkdir()
        requirement = root / "requirement.md"
        requirement.write_text(label, encoding="utf-8")
        raw_node = {"name": "Example", "type": "WorkflowStepNode", "desc": label, "planner_extra": {"label": label}}
        selected = node_generation.NodeGenerationRequest(
            graph_plan_path=str(root / "workflow.json"), requirement_md_path=str(requirement),
            requirement_text=label, node_meta=NodeMeta.from_dict(raw_node), raw_node=raw_node,
            target_path=root / "Example.py", markdown_path=docs / "Example.md",
            language=language, temperature=temperature, generate_markdown=generate_markdown,
            dependency_context=f"Generated file:\n# prior {label}",
            state={"request_label": label, "native_identity": object()},
        )
        assert type(selected.node_meta) is NodeMeta
        assert selected.node_meta.name == "Example"
        assert selected.raw_node is raw_node
        assert selected.node_format == {}
        assert not hasattr(selected, "artifacts")
        return selected

    workflow = node_generation.NodeGenerationWorkflow(planner, coder, auditor)
    engine = workflow
    try:
        assert type(engine) is node_generation.NodeGenerationWorkflow
        assert isinstance(engine, WorkflowEngine)
        component_classes = {node.STEP_ID: node for node in (Planner, Coder, Auditor)}
        assert len(configs) == 1
        assert configs[0][0] == component_classes
        assert all(configs[0][0][name] is cls for name, cls in component_classes.items())
        assert workflow._node_classes is configs[0][0]
        assert configs[0][1] is config
        assert engine.steps_meta == [cls.step_meta() for cls in component_classes.values()]
        for instances, component, cls in ((planners, planner, Planner), (coders, coder, Coder), (auditors, auditor, Auditor)):
            assert len(instances) == pool_size
            assert all(type(node) is cls and node is not component for node in instances)
            assert len({id(node) for node in instances}) == pool_size
            assert all("process_input" not in node.__dict__ for node in instances)
            assert all(node.instances is instances for node in instances)
        assert all(node.requests is requests and node.barrier is barrier for node in coders)
        assert all(node.audit is auditor.audit for node in auditors)
        # A prototype instance cannot be used as a dispatcher by native nodes.
        planner.process_input = coder.process_input = auditor.process_input = lambda *_a, **_kw: pytest.fail("prototype dispatch")
        first, second = request("first", 0.1), request("second", 0.7, "javascript")
        assert first is not second
        assert first.node_meta is not second.node_meta
        assert first.raw_node is not second.raw_node
        assert first.node_format is not second.node_format
        assert first.state is not second.state
        assert first.state["native_identity"] is not second.state["native_identity"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(workflow.run, item) for item in (first, second)]
            paths = [future.result(timeout=15) for future in futures]
        assert paths == [str(first.target_path), str(second.target_path)]
        assert sorted((Path(path).parent.name, temperature, language) for _, path, temperature, language in requests) == [
            ("first", 0.1, "python"), ("second", 0.7, "javascript"),
        ]
        if pool_size == 2:
            assert requests[0][0] is not requests[1][0]
        third = request("third", 0.4, generate_markdown=False)
        assert workflow.run(third).endswith("third/Example.py")
        assert results[-1].outputs["node_plan"].card == {"status": "skipped"}
        assert len({result.request_id for result in results}) == 3
        assert len(native_states) == 3
        assert len(planner_states) == 2
        for name in ("raw_node", "node_format", "state"):
            assert len({id(getattr(item, name)) for item in (first, second, third)}) == 3
        for item in (first, second, third):
            state, _, native_identity, writer_meta, auditor_meta = next(
                observed for observed in native_states if observed[1] == item.requirement_text
            )
            assert state is not item.state
            assert native_identity is item.state["native_identity"]
            assert writer_meta is item.node_meta
            assert auditor_meta is item.node_meta
            if item.generate_markdown:
                planner_state, _, planned_node = next(
                    observed for observed in planner_states if observed[1] == item.requirement_text
                )
                assert planner_state is state
                assert type(planned_node) is dict
                assert planned_node == item.raw_node
                assert planned_node is not item.raw_node
                assert planned_node["planner_extra"] is item.raw_node["planner_extra"]
                assert item.markdown_path.is_file()
            else:
                assert not item.markdown_path.exists()
            assert item.state == {"request_label": item.requirement_text, "native_identity": native_identity}
            assert item.node_format == {}
            assert item.dependency_context == f"Generated file:\n# prior {item.requirement_text}"
            assert not hasattr(item, "artifacts")  # The workflow never publishes builder-owned artifacts.
        assert not hasattr(workflow, "_last_result")
        assert workflow is engine
        assert (len(planners), len(coders), len(auditors)) == (pool_size,) * 3
        assert [(cls.STEP_ID, list(cls.DEPENDENCIES)) for cls in (Planner, Coder, Auditor)] == original_metadata
    finally:
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed
    workflow.close()
    with pytest.raises(RuntimeError, match="closed"):
        workflow.run(third)


@pytest.mark.parametrize("pool_size", [1, 2])
def test_generation_retains_dataclass_settings_and_restores_construction_scope(monkeypatch, pool_size):
    @dataclass
    class Planner(NodePlanElement):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    @dataclass
    class Coder(WorkflowStepNodeCoder):
        client: object = field(default_factory=object)
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    @dataclass
    class Auditor(_FakeAuditor):
        instances: list = field(default_factory=list)

        def __post_init__(self):
            super().__post_init__()
            self.instances.append(self)

    config = node_generation.NodeGenerationWorkflow.json_config()
    config["pool_size"] = pool_size
    monkeypatch.setattr(node_generation.NodeGenerationWorkflow, "json_config", staticmethod(lambda: config))
    workflows, engines = [], []
    try:
        for label in ("first", "second"):
            planners, coders, auditors = [], [], []
            planner = Planner(
                client=object(), instances=planners, system_prompt=f"{label} planner prompt",
                session_marking_prompt=f"{label} planner marking",
                output_path=f"/{label}/plan.md", overwrite=False, temperature=0.17, max_tokens=123,
            )
            coder = Coder(
                client=object(), instances=coders, system_prompt=f"{label} coder prompt",
                session_marking_prompt=f"{label} coder marking",
                timeout=4.5, use_streaming=False,
            )
            auditor = Auditor(audit=lambda *_a, **_kw: (True, []), instances=auditors)
            auditor.white_list.add(label)
            planners.clear()
            coders.clear()
            auditors.clear()
            snapshots = ((planner, planners), (coder, coders), (auditor, auditors))
            with node_settings({Planner.STEP_ID: {"system_prompt": "outer prompt"}}):
                workflow = node_generation.NodeGenerationWorkflow(planner, coder, auditor)
                workflows.append(workflow)
                engines.append(workflow)
                assert _FakePlanner().system_prompt == "outer prompt"
            assert _FakePlanner().system_prompt == NodePlanElement.system_prompt
            assert _FakeAuditor().audit is None
            for prototype, instances in snapshots:
                assert len(instances) == pool_size
                assert len({id(node) for node in instances}) == pool_size
                assert all(type(node) is type(prototype) and node is not prototype for node in instances)
                assert all(node.instances is instances for node in instances)
            for prototype, instances in ((planner, planners), (coder, coders)):
                assert all(node.client is prototype.client for node in instances)
                assert all(node.system_prompt == prototype.system_prompt for node in instances)
                assert all(node.system_prompt.count(prototype.session_marking_prompt) == 1 for node in instances)
                prompt_snapshot = prototype.system_prompt
                prototype.system_prompt = "late replacement"
                prototype.client = object()
                assert all(node.system_prompt == prompt_snapshot for node in instances)
                assert all(node.client is not prototype.client for node in instances)
            assert all(
                (node.output_path, node.overwrite, node.temperature, node.max_tokens)
                == (planner.output_path, False, 0.17, 123) for node in planners
            )
            assert all(node.timeout == 4.5 and node.use_streaming is False for node in coders)
            assert all(node.audit is auditor.audit for node in auditors)
            whitelist_snapshot = set(auditor.white_list)
            assert all(node.white_list == whitelist_snapshot and node.white_list is not auditor.white_list for node in auditors)
            assert len({id(node.white_list) for node in auditors}) == pool_size
            auditor.white_list.add("late replacement")
            auditor.audit = lambda *_a, **_kw: pytest.fail("late audit replacement")
            assert all(node.white_list == whitelist_snapshot and node.audit is not auditor.audit for node in auditors)
        assert workflows[0] is not workflows[1]
        assert workflows[0]._node_classes == workflows[1]._node_classes == {
            node.STEP_ID: node for node in (Planner, Coder, Auditor)
        }
    finally:
        for workflow in workflows:
            workflow.close()
    assert all(workflow.closed for workflow in workflows)
    assert all(engine.pool.closed for engine in engines)


def test_request_node_coder_uses_one_workflow_for_mixed_contracts(monkeypatch, tmp_path: Path) -> None:
    results = _capture_workflow_results(monkeypatch)
    requirement = tmp_path / "requirement.md"
    requirement.write_text("Collect and upload data.", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Collect", "type": "WorkflowStepNode", "meta_node_kind": "WorkflowStepNode",
         "desc": "Collect data", "depends": [], "ext_data": {"type": "none"}},
        {"name": "Upload", "type": "WorkflowFileNode", "meta_node_kind": "WorkflowFileNode",
         "desc": "Upload data", "depends": ["Collect"],
         "ext_data": {"type": "user_file_input", "remote_desc": "Archive uploaded bytes"}},
    ]}), encoding="utf-8")
    prompts: dict[str, str] = {}
    configs = []
    audited_kinds = []
    native_states = []

    @dataclass
    class Coder(RequestNodeFileCoder):
        prompts: dict[str, str] = field(default_factory=dict)

        def process_input(self, user_input, dependency_results, session_state):
            assert self.get_workflow_param().state is session_state
            assert session_state is not active_request.state
            assert session_state["native_identity"] is active_request.state["native_identity"]
            assert session_state["node_writer"]["node_meta"] is active_request.node_meta
            assert session_state["auditor"]["node_meta"] is active_request.node_meta
            assert "previous_request" not in session_state
            session_state["previous_request"] = True
            native_states.append((active_request, session_state))
            return super().process_input(user_input, dependency_results, session_state)

        def code_to_file(self, prompt, path, **kwargs):
            target = Path(path)
            self.prompts[target.stem] = prompt
            target.write_text(f"# generated {target.stem}\n", encoding="utf-8")
            return target

    def audit(_path, meta, **_kwargs):
        assert meta is active_request.node_meta
        audited_kinds.append(meta.meta_node_kind)
        return True, []

    real_build_pool = workflow_engine_module.build_workflow_pool

    def capture_config(node_classes, workflow_config):
        configs.append((node_classes, workflow_config))
        return real_build_pool(node_classes, workflow_config)

    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", capture_config)
    workflow = NodeGenerationWorkflow(
        node_planner=_FakePlanner(),
        node_coder=Coder(client=object(), prompts=prompts),
        node_auditor=_FakeAuditor(audit),
    )
    engine = workflow
    try:
        assert len(configs) == 1  # Construction happens before any node request.
        assert isinstance(engine, WorkflowEngine)
        component_classes = {"node_plan": _FakePlanner, "node_generate": Coder, "node_audit": _FakeAuditor}
        assert configs[0][0] == component_classes
        assert all(configs[0][0][name] is cls for name, cls in component_classes.items())
        assert workflow._node_classes is configs[0][0]

        docs = tmp_path / "node_docs"
        docs.mkdir()
        nodes = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
        collect = node_generation.NodeGenerationRequest(
            graph_plan_path=str(graph_path), requirement_md_path=str(requirement),
            requirement_text="Collect and upload data.", node_meta=NodeMeta.from_dict(nodes[0]),
            raw_node=nodes[0], target_path=tmp_path / "Collect.py", markdown_path=docs / "Collect.md",
            state={"native_identity": object()},
        )
        active_request = collect
        collect_path = workflow.run(collect)
        upload = node_generation.NodeGenerationRequest(
            graph_plan_path=str(graph_path), requirement_md_path=str(requirement),
            requirement_text="Collect and upload data.", node_meta=NodeMeta.from_dict(nodes[1]),
            raw_node=nodes[1], target_path=tmp_path / "Upload.py", markdown_path=docs / "Upload.md",
            dependency_context="### Collect\nGenerated file:\n# generated Collect\n",
            state={"native_identity": object()},
        )
        active_request = upload
        upload_path = workflow.run(upload)
        selected_requests = [collect, upload]
        generated = [collect_path, upload_path]
        assert generated == [str(tmp_path / f"{name}.py") for name in ("Collect", "Upload")]
        assert [request.node_meta.name for request in selected_requests] == ["Collect", "Upload"]
        assert all(type(request.node_meta) is NodeMeta for request in selected_requests)
        assert len({id(request) for request in selected_requests}) == 2
        assert len({id(request.raw_node) for request in selected_requests}) == 2
        assert len({id(request.node_format) for request in selected_requests}) == 2
        assert len({id(request.state) for request in selected_requests}) == 2
        assert all(request.node_amendment == "" for request in selected_requests)
        assert all(request.skip_node_planner is False for request in selected_requests)
        assert selected_requests[0].dependency_context == ""
        assert len(results) == 2
        assert audited_kinds == ["WorkflowStepNode", "WorkflowFileNode"]
        assert "Generate a WorkflowStepNode subclass" in prompts["Collect"]
        assert "Generate a WorkflowFileNode subclass" in prompts["Upload"]
        assert "file_bytes: bytes" in prompts["Upload"]
        assert "Archive uploaded bytes" in prompts["Upload"]
        assert "# generated Collect" in prompts["Upload"]
        assert "# generated Collect" in selected_requests[1].dependency_context
        assert selected_requests[1].dependency_context in prompts["Upload"]
        snapshots = [request.dependency_context for request in selected_requests]

        amended_upload = replace(
            upload, node_meta=NodeMeta.from_dict(upload.raw_node), raw_node=dict(upload.raw_node),
            node_format={}, state={"native_identity": object()}, generate_markdown=False,
            node_amendment="Retain remote persistence",
        )
        active_request = amended_upload
        amended_upload_path = workflow.run(amended_upload)
        amended = [amended_upload_path]
        selected_requests.append(amended_upload)
        assert amended == generated[1:]
        assert len(selected_requests) == 3
        assert [request.node_meta.name for request in selected_requests[2:]] == ["Upload"]
        assert [request.node_amendment for request in selected_requests[2:]] == ["Retain remote persistence"]
        for original, request in zip(selected_requests[1:2], selected_requests[2:]):
            assert request is not original
            assert request.node_meta is not original.node_meta
            assert request.raw_node is not original.raw_node
            assert request.node_format is not original.node_format
            assert request.state is not original.state
            assert request.state["native_identity"] is not original.state["native_identity"]
            assert request.target_path == original.target_path
            assert request.markdown_path == original.markdown_path
            assert request.generate_markdown is False
        assert "# generated Collect" in selected_requests[2].dependency_context
        assert [request.dependency_context for request in selected_requests[:2]] == snapshots
        assert "Preserve the WorkflowFileNode bytes-processing" in prompts["Upload"]
        assert "Archive uploaded bytes" in prompts["Upload"]
        assert len(configs) == 1
        assert len(results) == 3
        assert len(native_states) == 3
        assert [request for request, _ in native_states] == selected_requests
        assert audited_kinds == ["WorkflowStepNode", "WorkflowFileNode", "WorkflowFileNode"]
        assert len({result.request_id for result in results}) == 3
        assert all(set(result.outputs) == {"node_plan", "node_generate", "node_audit"} for result in results)
        assert all(result.outputs["node_audit"].derived["ok"] is True for result in results)
        assert all(result.outputs["node_plan"].card == {"status": "skipped"} for result in results[-1:])
        assert all(set(request.state) == {"native_identity"} for request in selected_requests)
        assert workflow is engine
        assert not any(
            isinstance(value, node_generation.NodeGenerationRequest) for value in vars(workflow).values()
        )
        assert not hasattr(workflow, "_last_result")
    finally:
        workflow.close()
    assert workflow.closed
    assert engine.pool.closed





