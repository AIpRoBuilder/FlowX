import json
from pathlib import Path
from types import SimpleNamespace

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder


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
