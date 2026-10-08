import json
from pathlib import Path

import pytest
from ag_ui_workflow import WorkflowStepNode

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.auditor.data import RuleViolation
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.worker.node_writer import WorkflowStepNodeCoder
from flowx_core.workflows import node_generation
from flowx_core.architect.node_planner import NodePlanElement


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeWriter(WorkflowStepNodeCoder):
    def __post_init__(self):
        WorkflowStepNode.__init__(self)


class _FakeAuditor(NodeAuditor):
    def __init__(self, audit):
        super().__init__()
        self._audit = audit

    def audit_file(self, file_path, node_meta=None, graph_plan_path=None):
        return self._audit(file_path, node_meta, graph_plan_path=graph_plan_path)


def _make_builder(monkeypatch, root: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanElement", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    return AgentBuilder(api_key="key", model="model", provider="provider", root_dir=str(root))


def test_node_build_service_runs_single_node_workflow_per_kind(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Standalone", "type": "WorkflowStepNode", "desc": "standalone",
         "depends": [], "ext_data": {"type": "none"}},
    ]}), encoding="utf-8")
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Build standalone node", encoding="utf-8")

    class Planner(NodePlanElement):
        def __post_init__(self):
            WorkflowStepNode.__init__(self)

        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("# Standalone plan", encoding="utf-8")
            return Path(path)

    class Coder(_FakeWriter):
        def code_to_file(self, prompt, path, **kwargs):
            Path(path).write_text("class Standalone: pass\n", encoding="utf-8")
            return Path(path)

    builder.graph_plan_path = str(graph_path)
    builder.requirement_md_path = str(requirement_path)
    builder.node_planner = Planner()
    builder._make_node_coder = lambda _meta: Coder()
    builder.node_auditor = _FakeAuditor(lambda *_args, **_kwargs: (True, []))
    service = builder._node_build_service
    context = service.run()

    assert context.selected_node_names == ["Standalone"]
    assert context.artifacts["Standalone"]["node_markdown"] == "# Standalone plan"
    assert context.artifacts["Standalone"]["node_file"] == "class Standalone: pass\n"
    assert service.workflows["WorkflowStepNode"]._engine.session.step_outputs["node_audit"].derived["ok"] is True


def test_node_generation_workflow_carries_prior_artifacts_and_routes_requested_workflows(
    monkeypatch, tmp_path: Path
) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Build a small data pipeline.\n", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "Collect",
                        "type": "WorkflowStepNode",
                        "desc": "Collect input",
                        "enable": True,
                        "depends": [],
                        "ext_data": {"type": "user_input", "desc": "collect input"},
                        "inputs_format": {"query": "String"},
                    },
                    {
                        "name": "Transform",
                        "type": "WorkflowStepNode",
                        "desc": "Transform data",
                        "enable": True,
                        "depends": ["Collect"],
                        "ext_data": {"type": "none", "desc": "none"},
                    },
                    {
                        "name": "Publish",
                        "type": "WorkflowStepNode",
                        "desc": "Publish results",
                        "enable": True,
                        "depends": ["Transform"],
                        "ext_data": {"type": "none", "desc": "none"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    class _FakeNodePlanElement:
        def process_input(self, user_input, dependency_results, session_state):
            path = Path(self.output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            request = session_state["node_plan_request"]
            path.write_text(
                f"# {request['node']['name']} plan\n{request['requirement_text']}", encoding="utf-8"
            )
            return node_generation.StepRunOutput(derived={"markdown_path": str(path.resolve())})

    builder.node_planner = _FakeNodePlanElement()
    generated_prompt_contexts: dict[str, str] = {}

    class _FakeNodeCoder(_FakeWriter):
        def write_node_from_requirement(
            self, node_name, node_meta, requirement_md_path, output_path, **kwargs
        ):
            generated_prompt_contexts[node_name] = kwargs["additional_generation_context"]
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# generated {node_name}\n", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("unexpected audit repair")

    builder._make_node_coder = lambda _meta: _FakeNodeCoder()
    builder.node_auditor = _FakeAuditor(lambda *_args, **_kwargs: (True, []))
    service = builder._node_build_service
    invoked_workflows: list[str] = []

    def run_review(*, node_name, artifact, context):
        invoked_workflows.append(node_name)
        assert artifact["node_file_path"].endswith("Publish.py")
        return {"reviewed": True}

    context = service.run(
        graph_plan_path=str(graph_path),
        requirement_md_path=str(requirement_path),
        run_nodes={"Collect": True, "Transform": False, "Publish": True},
        generate_markdowns=True,
        workflow_requests={"Publish": ["review"]},
        workflow_registry={"review": run_review},
    )

    assert context.selected_node_names == ["Collect", "Publish"]
    assert context.skipped_node_names == ["Transform"]
    assert set(context.artifacts) == {"Collect", "Publish"}
    assert Path(context.artifacts["Collect"]["node_markdown_path"]).is_file()
    assert Path(context.artifacts["Collect"]["node_file_path"]).is_file()
    assert "# generated Collect" in generated_prompt_contexts["Publish"]
    assert "# Collect plan" in generated_prompt_contexts["Publish"]
    assert "Build a small data pipeline." in generated_prompt_contexts["Publish"]
    assert context.node_formats["Collect"]["user_input_format"] == {"query": "string"}
    assert '"query": "string"' in generated_prompt_contexts["Collect"]
    assert invoked_workflows == ["Publish"]
    assert context.artifacts["Publish"]["workflow_outputs"] == {"review": {"reviewed": True}}


def test_node_generation_has_three_ordered_stages_and_publishes_explicit_amendment(
    monkeypatch, tmp_path: Path
) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate one step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "First", "type": "WorkflowStepNode", "desc": "first", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
        {"name": "Second", "type": "WorkflowStepNode", "desc": "second", "enable": True,
         "depends": ["First"], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)
    events: list[str] = []
    configs = []
    real_init_engine = node_generation.init_pipeline_engine

    def capture_config(steps, dependencies):
        configs.append((dict(steps), dict(dependencies)))
        return real_init_engine(steps, dependencies)

    monkeypatch.setattr(node_generation, "init_pipeline_engine", capture_config)
    (tmp_path / "Second.py").write_text("# existing Second", encoding="utf-8")

    class FakePlanner:
        def process_input(self, user_input, dependency_results, session_state):
            name = session_state["node_plan_request"]["node"]["name"]
            events.append(f"{name}:plan")
            path = Path(self.output_path)
            path.write_text(f"# {name} plan", encoding="utf-8")
            return node_generation.StepRunOutput(derived={"markdown_path": str(path.resolve())})

    builder.node_planner = FakePlanner()

    class FakeCoder(_FakeWriter):
        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            events.append(f"{name}:write")
            path = Path(output_path)
            path.write_text(f"# initial {name}", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, file_path, feedback, **kwargs):
            events.append("Second:repair")
            assert feedback == "Apply requested fix"
            Path(file_path).write_text("# repaired Second", encoding="utf-8")
            return Path(file_path)

    builder._make_node_coder = lambda _meta: FakeCoder()

    def audit(file_path, _meta, **kwargs):
        name = Path(file_path).stem
        events.append(f"{name}:audit")
        if name == "Second" and "repaired" not in Path(file_path).read_text(encoding="utf-8"):
            return False, [RuleViolation(class_name=name, rule="bad_code", detail="repair needed", lineno=1)]
        return True, []

    builder.node_auditor = _FakeAuditor(audit)
    service = builder._node_build_service
    context = service.run(workflow_name="stages", node_amendments={"Second": "Apply requested fix"}, workflow_requests={"Second": "review"}, workflow_registry={
        "review": lambda *, artifact, **kwargs: events.append("Second:review") or artifact["node_file"]
    })

    for steps, dependencies in configs:
        assert list(steps) == ["node_plan", "node_generate", "node_audit"]
        assert dependencies == {"node_generate": ["node_plan"], "node_audit": ["node_generate"]}
    assert events == [
        "First:plan", "First:write", "First:audit",
        "Second:plan", "Second:repair", "Second:audit", "Second:review",
    ]
    assert context.artifacts["Second"]["node_file"] == "# repaired Second"
    assert context.artifacts["Second"]["workflow_outputs"] == {"review": "# repaired Second"}
    assert service.workflows["WorkflowStepNode"].node_coder is not None
    assert builder.node_location_map["Second"] == context.artifacts["Second"]["node_file_path"]


def test_failed_audit_does_not_publish_generated_node(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate a step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Broken", "type": "WorkflowStepNode", "desc": "broken", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    class FakeCoder(_FakeWriter):
        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            path = Path(output_path)
            path.write_text("# invalid", encoding="utf-8")
            return path

    builder._make_node_coder = lambda _meta: FakeCoder()
    audit_calls = []

    def audit(*_args, **_kwargs):
        audit_calls.append(1)
        return False, [RuleViolation(class_name="Broken", rule="bad_code", detail="invalid", lineno=1)]

    builder.node_auditor = _FakeAuditor(audit)
    service = builder._node_build_service

    with pytest.raises(RuntimeError, match="bad_code"):
        service.run(workflow_name="failed", generate_markdowns=False, reset_mappings=True)

    assert service.contexts["failed"] is not None
    assert "Broken" not in service.contexts["failed"].artifacts
    assert "Broken" not in builder.node_location_map
    assert "Broken" not in builder.node_coder_map
    assert audit_calls == [1]
