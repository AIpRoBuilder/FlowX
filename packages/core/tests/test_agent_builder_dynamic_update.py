"""Test explicit node requests, repair routing, and workflow lifecycle cleanup."""
# Modified 2026-10-14: explicitly inject replacement components into builder services.
# Modified 2026-10-14: exercise eager workflows with offline request-local inputs.
# Modified 2026-10-15: verify flat generation batches and fresh repair request outputs.
# Modified 2026-10-15: verify minimal request APIs and builder-local workflow routing.
# Modified 2026-10-15: capture complete per-node requests and builder-owned batch publications.
# Modified 2026-10-15: reject batch generation and separate explicit node requests from lazy log repair.
# Modified 2026-10-16: drive the builder-owned workflows directly now that service wrappers are gone.

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from unittest.mock import Mock

import pytest
from ag_ui_workflow import WorkflowStepNode
import flowx_core.agent_builder as agent_builder_module
import flowx_core.agent_workflow_engine as agent_workflow_engine_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.agent_intent import INTENT_SYSTEM_PROMPT
from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools._node_settings import apply_node_settings
from flowx_core.tools.node_formats import collect_node_formats
from flowx_core.worker.node_writer import WorkflowStepNodeCoder
from flowx_core.workflows.node_generation import NodeGenerationRequest, NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import (
    NodeRuntimeModifierPipeline,
    NodeRuntimeModifierRequest,
)
import flowx_core.workflows.node_runtime_modifier as node_runtime_modifier_module


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeWorkflow:
    def __init__(self, **_kwargs):
        self.run = Mock(side_effect=AssertionError("unused workflow must not run"))
        self.close = Mock()


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
    for name in (
        "GraphGenerationWorkflow", "NodeGenerationWorkflow",
        "NodeRuntimeModifierPipeline", "MainEntrypointGenerationWorkflow",
    ):
        monkeypatch.setattr(agent_builder_module, name, _FakeWorkflow)
    return AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )


def _replace_node_workflow(builder, node_coder) -> NodeGenerationWorkflow:
    builder._node_workflow.close()
    builder.node_coder = node_coder
    builder.node_auditor = _FakeAuditor()
    builder._node_workflow = NodeGenerationWorkflow(
        node_planner=builder.node_planner,
        node_coder=builder.node_coder,
        node_auditor=builder.node_auditor,
    )
    return builder._node_workflow


def _replace_modifier_pipeline(
    builder, *, node_test_writer, node_writer, run_subprocess,
) -> NodeRuntimeModifierPipeline:
    builder._modifier_pipeline.close()
    builder.node_test_writer = node_test_writer
    builder.node_modifier_writer = node_writer
    builder._modifier_pipeline = NodeRuntimeModifierPipeline(
        node_test_writer=node_test_writer,
        node_writer=node_writer,
        run_subprocess=run_subprocess,
        timeout_expired=builder._subprocess_timeout_expired(),
    )
    return builder._modifier_pipeline


def _assert_no_request_cache(workflow):
    def contains_request(value):
        if isinstance(value, (NodeGenerationRequest, NodeRuntimeModifierRequest)):
            return True
        if isinstance(value, dict):
            return any(contains_request(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(contains_request(item) for item in value)
        return False

    assert not any(contains_request(value) for value in vars(workflow).values())


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
        builder._graph_workflow,
        builder._node_workflow,
        builder._modifier_pipeline,
        builder._main_workflow,
    ]
    builder.close()
    builder.close()
    assert len({id(workflow) for workflow in workflows}) == 4
    for workflow in workflows:
        assert workflow.close.call_count == 2


def _write_graph(graph_path: Path, nodes: list[dict]) -> None:
    graph_path.write_text(json.dumps({"nodes": nodes}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_node_generation_workflow_generates_and_amends_explicit_requests(monkeypatch, tmp_path):
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
    workflow = _replace_node_workflow(builder, fake_node_coder)
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

    generated_path = workflow.run(request)

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

    amended_path = workflow.run(amendment_request)

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
    # Direct workflow requests do not create caller-owned test files.
    assert not (graph_dir / "tests").exists()
    assert not (project_root / "tests").exists()
    _assert_no_request_cache(workflow)
    builder._modifier_pipeline.run.assert_not_called()
    builder.close()


def test_modifier_pipeline_reuses_one_eager_workflow_for_named_node_requests(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    test_writer = _FakeTestWriter()
    run_subprocess = Mock(side_effect=AssertionError("node tests must be skipped"))
    workflow = _replace_modifier_pipeline(
        builder,
        node_test_writer=test_writer,
        node_writer=_FakeModifierWriter(),
        run_subprocess=run_subprocess,
    )
    engine = workflow
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
        context = workflow.run(request)
        contexts.append(context)
        _assert_fresh_modifier_request(request, context)
        assert workflow is engine
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
    _assert_no_request_cache(workflow)
    assert len(test_writer.write_calls) == 4
    assert test_writer.root_dir_path == ""
    run_subprocess.assert_not_called()
    builder.close()


def test_collect_node_formats_collects_inputs_and_backend_card_schema(tmp_path):
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

    formats = collect_node_formats(Graph(str(graph_path)), root_dir=tmp_path)

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


def test_builder_publishes_workflows_for_the_agent_engine_and_closes_it(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    graph_workflow = builder._graph_workflow
    main_workflow = builder._main_workflow
    assert agent_workflow_engine_module.GRAPH_GENERATION_WORKFLOW is graph_workflow
    assert agent_workflow_engine_module.MAIN_ENTRYPOINT_GENERATION_WORKFLOW is main_workflow

    engine = builder.engine

    assert builder.engine is engine
    assert engine.storage_dir == (tmp_path / "workflows").resolve()
    assert engine.runtime() is builder._runtime_service
    builder.close()
    assert agent_workflow_engine_module.GRAPH_GENERATION_WORKFLOW is None
    assert agent_workflow_engine_module.MAIN_ENTRYPOINT_GENERATION_WORKFLOW is None
    with pytest.raises(RuntimeError, match="closed"):
        engine.build_workflow("anything")
    builder.close()


def test_builder_judge_uses_the_current_llm_settings_and_the_intent_prompt(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    created = []

    class _Coder:
        def __init__(self, **settings):
            created.append(settings)

        def generate_code(self, prompt, **options):
            return f"{prompt}|{options['temperature']}"

    monkeypatch.setattr(agent_builder_module, "Coder", _Coder)
    builder.reset_llm_config(model="other-model")

    assert builder.engine.judge("classify") == "classify|0.0"
    assert created == [{
        "api_key": "key", "model": "other-model", "provider": "provider",
        "system_prompt": INTENT_SYSTEM_PROMPT,
    }]
    builder.close()


def test_builder_build_functions_delegate_to_the_engine(monkeypatch, tmp_path):
    builder = _make_builder(monkeypatch, tmp_path)
    builder._agent = Mock()

    for name, args, kwargs in (
        ("build_workflow_json", ("requirement",), {"workflow_id": "w"}),
        ("build_nodes", ("w", {"Node": "change"}), {}),
        ("build_main_entrypoint", ("w",), {"amendment": "change"}),
        ("build_workflow", ("request",), {"max_retries": 2}),
        ("evolve", ("w", "change"), {"target": "workflow"}),
        ("run_service", ("w",), {}),
    ):
        assert getattr(builder, name)(*args, **kwargs) is getattr(builder._agent, name).return_value
        getattr(builder._agent, name).assert_called_once_with(*args, **kwargs)
    builder.stop_service("w")
    builder._agent.stop_service.assert_called_once_with("w")
    builder._agent.close.assert_not_called()
    agent = builder._agent
    builder.close()
    agent.close.assert_called_once_with()