import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.auditor.data import RuleViolation
from flowx_core.workflows import node_generation


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _make_builder(monkeypatch, root: Path) -> AgentBuilder:
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    return AgentBuilder(api_key="key", model="model", provider="provider", root_dir=str(root))


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

    class _FakeNodePlanner:
        def plan_each(self, *, requirement_text, graph_plan_text, output_dir, **kwargs):
            selected_node = json.loads(graph_plan_text)["nodes"][0]
            path = Path(output_dir) / f"{selected_node['name']}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# {selected_node['name']} plan\n{requirement_text}", encoding="utf-8")
            return [path]

        def _node_context(self, node, index):
            return f"### Node {index}: {node['name']}"

        def code_to_file(self, prompt, file_path, **kwargs):
            Path(file_path).write_text(prompt, encoding="utf-8")
            return Path(file_path)

    builder.node_planner = _FakeNodePlanner()
    generated_prompt_contexts: dict[str, str] = {}

    class _FakeNodeCoder:
        root_dir_path = ""

        def write_node_from_requirement(
            self, node_name, node_meta, requirement_md_path, output_path, **kwargs
        ):
            generated_prompt_contexts[node_name] = self.additional_generation_context
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"# generated {node_name}\n", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, *_args, **_kwargs):
            raise AssertionError("unexpected audit repair")

    builder._make_node_coder = lambda _meta: _FakeNodeCoder()
    builder.node_auditor = SimpleNamespace(
        audit_node_file=lambda *_args, **_kwargs: (True, [])
    )
    workflow = builder._node_build_service.get_or_create_workflow("default")

    invoked_workflows: list[str] = []

    def run_review(*, node_name, artifact, context, builder):
        invoked_workflows.append(node_name)
        assert artifact["node_file_path"].endswith("Publish.py")
        return {"reviewed": True}

    context = workflow.run(
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
    assert "# Collect plan" in generated_prompt_contexts["Collect"]
    assert "# generated Collect" in generated_prompt_contexts["Publish"]
    assert "# Collect plan" in generated_prompt_contexts["Publish"]
    assert "Build a small data pipeline." in generated_prompt_contexts["Publish"]
    assert context.node_formats["Collect"]["user_input_format"] == {"query": "string"}
    assert '"query": "string"' in generated_prompt_contexts["Collect"]
    assert invoked_workflows == ["Publish"]
    assert context.artifacts["Publish"]["workflow_outputs"] == {"review": {"reviewed": True}}


def test_node_generation_has_three_ordered_stages_and_publishes_repaired_code(
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
    real_build_config = node_generation.build_workflow_config

    def capture_config(steps, dependencies):
        configs.append((dict(steps), dict(dependencies)))
        return real_build_config(steps, dependencies)

    monkeypatch.setattr(node_generation, "build_workflow_config", capture_config)

    class FakePlanner:
        def plan_each(self, *, graph_plan_text, output_dir, **kwargs):
            name = json.loads(graph_plan_text)["nodes"][0]["name"]
            events.append(f"{name}:plan")
            path = Path(output_dir) / f"{name}.md"
            path.write_text(f"# {name} plan", encoding="utf-8")
            return [path]

    builder.node_planner = FakePlanner()

    class FakeCoder:
        root_dir_path = ""

        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            events.append(f"{name}:write")
            path = Path(output_path)
            path.write_text(f"# initial {name}", encoding="utf-8")
            return path

        def amend_code_with_feedback(self, file_path, feedback, **kwargs):
            events.append("Second:repair")
            assert "bad_code" in feedback
            Path(file_path).write_text("# repaired Second", encoding="utf-8")

    builder._make_node_coder = lambda _meta: FakeCoder()

    def audit(file_path, _meta, **kwargs):
        name = Path(file_path).stem
        events.append(f"{name}:audit")
        if name == "Second" and "repaired" not in Path(file_path).read_text(encoding="utf-8"):
            return False, [RuleViolation(class_name=name, rule="bad_code", detail="repair needed", lineno=1)]
        return True, []

    builder.node_auditor = SimpleNamespace(audit_node_file=audit)
    workflow = builder._node_build_service.get_or_create_workflow("stages")
    context = workflow.run(workflow_requests={"Second": "review"}, workflow_registry={
        "review": lambda *, artifact, **kwargs: events.append("Second:review") or artifact["node_file"]
    })

    steps, dependencies = configs[0]
    assert list(steps) == [
        "First::plan", "First::write", "First::audit",
        "Second::plan", "Second::write", "Second::audit",
    ]
    assert dependencies["First::write"] == ["First::plan"]
    assert dependencies["First::audit"] == ["First::write"]
    assert dependencies["Second::plan"] == ["First::audit"]
    assert dependencies["Second::audit"] == ["Second::write"]
    assert events == [
        "First:plan", "First:write", "First:audit",
        "Second:plan", "Second:write", "Second:audit", "Second:repair",
        "Second:audit", "Second:review",
    ]
    assert context.artifacts["Second"]["node_file"] == "# repaired Second"
    assert context.artifacts["Second"]["workflow_outputs"] == {"review": "# repaired Second"}
    assert builder.node_coder_map["Second"] is workflow.writers["Second"]._current_coder
    assert builder.node_location_map["Second"] == workflow.nodes["Second"].last_generated_path


def test_failed_audit_does_not_publish_generated_node(monkeypatch, tmp_path: Path) -> None:
    builder = _make_builder(monkeypatch, tmp_path)
    builder.max_audit_rounds = 1
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("Generate a step", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Broken", "type": "WorkflowStepNode", "desc": "broken", "enable": True,
         "depends": [], "ext_data": {"type": "none", "desc": "none"}},
    ]}), encoding="utf-8")
    builder.requirement_md_path = str(requirement_path)
    builder.graph_plan_path = str(graph_path)

    class FakeCoder:
        root_dir_path = ""

        def write_node_from_requirement(self, name, _meta, _requirement, output_path, **kwargs):
            path = Path(output_path)
            path.write_text("# invalid", encoding="utf-8")
            return path

    builder._make_node_coder = lambda _meta: FakeCoder()
    builder.node_auditor = SimpleNamespace(audit_node_file=lambda *_args, **_kwargs: (
        False, [RuleViolation(class_name="Broken", rule="bad_code", detail="invalid", lineno=1)]
    ))
    workflow = builder._node_build_service.get_or_create_workflow("failed")

    with pytest.raises(RuntimeError, match="bad_code"):
        workflow.run(generate_markdowns=False, reset_mappings=True)

    assert workflow.context is not None
    assert "Broken" not in workflow.context.artifacts
    assert "Broken" not in builder.node_location_map
    assert "Broken" not in builder.node_coder_map
