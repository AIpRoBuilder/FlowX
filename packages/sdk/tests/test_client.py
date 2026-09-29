from pathlib import Path

import flowx_sdk.client as client_module


def test_sdk_uses_node_generation_workflows_for_create_and_update(monkeypatch, tmp_path):
    calls = []

    class FakeBuilder:
        def __init__(self, **kwargs):
            self.root_dir = kwargs["root_dir"]
            self.requirement_md_path = None
            self.graph_plan_path = None
            self.workflow_json_path = None

        def analyze_requirement(self, *, requirement_text):
            calls.append(("requirement", requirement_text))
            self.requirement_md_path = str(Path(self.root_dir) / "requirement.md")
            return self.requirement_md_path

        def plan_graph(self, **kwargs):
            self.graph_plan_path = str(Path(self.root_dir) / "workflow.json")
            return self.graph_plan_path

        def generate_nodes(self, **kwargs):
            calls.append(("generate_nodes", kwargs))
            return [str(Path(self.root_dir) / "Collect.py")]

        def _sync_workflow_graph_json(self, **kwargs):
            self.workflow_json_path = self.graph_plan_path
            return self.workflow_json_path

        def generate_main_entrypoint(self, *args, **kwargs):
            return str(Path(self.root_dir) / "main.py")

        def amend_workflow_json(self, **kwargs):
            return self.graph_plan_path

    monkeypatch.setattr(client_module, "AgentBuilder", FakeBuilder)
    client = client_module.FlowXClient(workspace=tmp_path, api_key="key")
    client.create_workflow("demo", "Collect input", backend_port=8123)
    client.update_workflow_node("demo", "Collect", "Update input")

    creation = calls[1][1]
    update = calls[2][1]
    assert creation["workflow_name"] == "demo"
    assert creation["generate_markdowns"] is True
    assert update["node_names"] == ["Collect"]
    assert update["workflow_name"] == "demo"
    assert update["markdown_amendments"] == {"Collect": "Update input"}
