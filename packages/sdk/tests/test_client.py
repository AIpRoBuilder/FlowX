from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import flowx_sdk
import flowx_sdk.client as client_module
from flowx_core.agent_intent import AgentIntent, StagePlan
from flowx_core.agent_workflow_engine import AgentResult, StageOutcome
from flowx_core.dynamic_workflow_orchestrator import NodeRepairInput


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    for name in ("FLOWX_DEFAULT_WORKSPACE", "FLOWX_LLM_API_KEY", "OPENAI_API_KEY", "FLOWX_LLM_MODEL", "FLOWX_LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    builder = Mock(name="builder")
    constructor = Mock(return_value=builder)
    monkeypatch.setattr(client_module, "AgentBuilder", constructor)
    client = client_module.FlowXClient(workspace=tmp_path, api_key="key", model="m", provider="p")
    return SimpleNamespace(client=client, builder=builder, constructor=constructor, workspace=tmp_path.resolve())


def test_builder_is_created_lazily_once_with_the_workspace_as_root(sdk):
    sdk.constructor.assert_not_called()

    assert sdk.client.builder is sdk.client.builder is sdk.builder

    sdk.constructor.assert_called_once_with(
        api_key="key", model="m", provider="p", root_dir=str(sdk.workspace),
    )


def test_llm_settings_fall_back_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWX_LLM_API_KEY", "env-key")
    monkeypatch.setenv("FLOWX_LLM_MODEL", "env-model")
    monkeypatch.setenv("FLOWX_LLM_PROVIDER", "env-provider")
    monkeypatch.setenv("FLOWX_DEFAULT_WORKSPACE", str(tmp_path / "ws"))

    client = client_module.FlowXClient()

    assert (client.api_key, client.model, client.provider) == ("env-key", "env-model", "env-provider")
    assert client.workspace == (tmp_path / "ws").resolve()
    assert client.workspace.is_dir()


def test_build_methods_delegate_to_the_engine_api(sdk):
    builder, client = sdk.builder, sdk.client

    client.build_workflow_json("  Triage tickets  ", temperature=0.1)
    client.build_workflow_json("Add priority", workflow_id="wf1")
    client.build_nodes("wf1")
    client.build_nodes("wf1", {"Reply": "Escalate urgent tickets"}, cascade=True)
    client.build_main_entrypoint("wf1", "Use port 9000", fastapi_port=9000)
    client.build_workflow("Add a node", workflow_id="wf1", logs=("trace",))

    assert builder.build_workflow_json.call_args_list[0].args == ("Triage tickets",)
    assert builder.build_workflow_json.call_args_list[0].kwargs == {"workflow_id": None, "temperature": 0.1}
    assert builder.build_workflow_json.call_args_list[1].kwargs == {"workflow_id": "wf1"}
    assert [c.args for c in builder.build_nodes.call_args_list] == [("wf1", None), ("wf1", {"Reply": "Escalate urgent tickets"})]
    assert builder.build_nodes.call_args_list[1].kwargs == {"cascade": True}
    builder.build_main_entrypoint.assert_called_once_with("wf1", "Use port 9000", fastapi_port=9000)
    builder.build_workflow.assert_called_once_with("Add a node", workflow_id="wf1", logs=("trace",))


def test_guided_repair_callback_is_merged_into_node_options(sdk):
    builder, client = sdk.builder, sdk.client
    callback = lambda context: f"guide {context.node_name}"
    previous = lambda context: ""

    client.build_nodes("wf1", repair_input_callback=callback)
    assert builder.build_nodes.call_args.kwargs == {
        "node_options": {"repair_input_callback": callback},
    }

    builder.reset_mock()
    client.build_workflow(
        "Add a node", workflow_id="wf1", repair_input_callback=callback,
        node_options={"max_repair_retries": 0, "repair_input_callback": previous},
    )
    assert builder.build_workflow.call_args.kwargs == {
        "workflow_id": "wf1",
        "node_options": {"max_repair_retries": 0, "repair_input_callback": callback},
    }

    builder.reset_mock()
    client.evolve("wf1", "Fix the node", repair_input_callback=callback)
    assert builder.evolve.call_args.kwargs == {
        "target": None,
        "node_options": {"repair_input_callback": callback},
    }

    builder.reset_mock()
    client.build_nodes("wf1", node_options={"max_repair_retries": 1})
    assert builder.build_nodes.call_args.kwargs == {
        "node_options": {"max_repair_retries": 1},
    }


def test_sdk_exports_the_repair_input_context():
    assert flowx_sdk.NodeRepairInput is NodeRepairInput
    assert NodeRepairInput(
        node_name="Collect", attempt=0, test_log="failed", test_result={"ok": False},
    ).node_name == "Collect"


def test_evolve_and_list_nodes_delegate_to_the_engine(sdk):
    builder, client = sdk.builder, sdk.client
    builder.engine.list_nodes.return_value = [{"step_id": "Reply", "node_id": "n1", "version": 1}]

    client.evolve("wf1", "  Add a summary  ", target="workflow", cascade=False)
    client.evolve("wf1", "Escalate urgent tickets")

    assert builder.evolve.call_args_list[0].args == ("wf1", "Add a summary")
    assert builder.evolve.call_args_list[0].kwargs == {"target": "workflow", "cascade": False}
    assert builder.evolve.call_args_list[1].kwargs == {"target": None}
    assert client.list_nodes("wf1") == [{"step_id": "Reply", "node_id": "n1", "version": 1}]
    builder.engine.list_nodes.assert_called_once_with("wf1")
    with pytest.raises(ValueError, match="command"):
        client.evolve("wf1", "  ")


def test_build_workflow_json_rejects_an_empty_prompt(sdk):
    with pytest.raises(ValueError, match="prompt"):
        sdk.client.build_workflow_json("   ")

    sdk.builder.build_workflow_json.assert_not_called()


def test_service_and_inspection_methods(sdk):
    builder = sdk.builder
    builder.run_service.return_value = {"pid": 7}
    builder.engine.catalog.return_value = {"wf1": {}}
    builder.engine.artifacts.return_value = {"workflow_id": "wf1"}

    assert sdk.client.run_service("wf1") == {"pid": 7}
    sdk.client.stop_service("wf1")
    assert sdk.client.workflows() == {"wf1": {}}
    assert sdk.client.artifacts("wf1") == {"workflow_id": "wf1"}

    builder.run_service.assert_called_once_with("wf1")
    builder.stop_service.assert_called_once_with("wf1")
    builder.engine.artifacts.assert_called_once_with("wf1")


def test_close_releases_the_builder_once_and_a_new_one_is_created_afterwards(sdk):
    with sdk.client as client:
        assert client.builder is sdk.builder

    sdk.builder.close.assert_called_once_with()
    sdk.client.close()
    sdk.builder.close.assert_called_once_with()
    sdk.client.builder
    assert sdk.constructor.call_count == 2


def test_close_without_use_does_not_create_a_builder(sdk):
    sdk.client.close()

    sdk.constructor.assert_not_called()


def test_result_to_dict_flattens_nested_dataclasses():
    result = AgentResult(
        workflow_id="wf1", ok=False,
        intent=AgentIntent(workflow_id="wf1", graph=StagePlan("modify", "Add priority")),
        stages={"graph": StageOutcome("graph", "completed", 1, changed=True), "nodes": StageOutcome("nodes", "failed", 2, "boom")},
        errors={"nodes/Reply": "boom"}, logs={"main_run": "ok"}, artifacts={"workflow_id": "wf1"},
    )

    payload = client_module.result_to_dict(result)

    assert payload["ok"] is False and payload["workflow_id"] == "wf1"
    assert payload["intent"]["graph"]["mode"] == "modify"
    assert payload["stages"]["nodes"]["status"] == "failed" and payload["stages"]["nodes"]["error"] == "boom"
    assert payload["errors"] == {"nodes/Reply": "boom"}
    assert payload["service"] is None
