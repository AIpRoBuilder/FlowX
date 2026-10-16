from pathlib import Path
from unittest.mock import Mock

import pytest

import flowx_sdk.client as client_module


_BATCH_GENERATION_ERROR = (
    "Batch node generation is not implemented; use build_nodes() to generate a workflow's nodes."
)


@pytest.fixture
def deferred_batch_client(monkeypatch, tmp_path):
    root_dir = tmp_path / "demo"
    builder = Mock(
        root_dir=str(root_dir),
        requirement_md_path=str(root_dir / "requirement.md"),
        graph_plan_path=str(root_dir / "workflow.json"),
        workflow_json_path=str(root_dir / "workflow.json"),
    )
    builder.analyze_requirement.return_value = builder.requirement_md_path
    builder.plan_graph.return_value = builder.graph_plan_path
    builder.amend_workflow_json.return_value = builder.workflow_json_path
    generation_error = NotImplementedError(_BATCH_GENERATION_ERROR)
    builder.generate_nodes.side_effect = generation_error
    monkeypatch.setattr(client_module, "AgentBuilder", Mock(return_value=builder))
    client = client_module.FlowXClient(workspace=tmp_path, api_key="stub")
    return client, builder, generation_error


def test_sdk_create_propagates_deferred_batch_error_without_sync_or_main(deferred_batch_client):
    client, builder, generation_error = deferred_batch_client

    with pytest.raises(NotImplementedError) as caught:
        client.create_workflow("demo", "Collect input", backend_port=8123)

    assert caught.value is generation_error
    assert str(caught.value) == _BATCH_GENERATION_ERROR
    assert [call[0] for call in builder.method_calls] == [
        "analyze_requirement", "plan_graph", "generate_nodes",
    ]
    builder.analyze_requirement.assert_called_once_with(requirement_text="Collect input")
    builder.plan_graph.assert_called_once_with(
        requirement_md_path=builder.requirement_md_path,
        graph_plan_filename="workflow.json",
        temperature=0.3,
    )
    builder.generate_nodes.assert_called_once()
    builder._sync_workflow_graph_json.assert_not_called()
    builder.generate_main_entrypoint.assert_not_called()


def test_sdk_update_propagates_deferred_batch_error_without_sync_or_main(deferred_batch_client):
    client, builder, generation_error = deferred_batch_client

    with pytest.raises(NotImplementedError) as caught:
        client.update_workflow_node("demo", "Collect", "Update input")

    assert caught.value is generation_error
    assert str(caught.value) == _BATCH_GENERATION_ERROR
    assert [call[0] for call in builder.method_calls] == [
        "amend_workflow_json", "generate_nodes",
    ]
    builder.amend_workflow_json.assert_called_once_with(
        user_prompt="Update input",
        workflow_json_path=builder.workflow_json_path,
        temperature=0.2,
    )
    builder.generate_nodes.assert_called_once()
    builder._sync_workflow_graph_json.assert_not_called()
    builder.generate_main_entrypoint.assert_not_called()


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
