# SPDX-License-Identifier: Apache-2.0
"""Exercise registered MCP tools using fake handles and fake HTTP only."""

import json
from types import ModuleType, SimpleNamespace
import sys
import urllib.error
from unittest.mock import Mock

import pytest


_BATCH_GENERATION_ERROR = (
    "Batch node generation is not implemented; use build_nodes() to generate a workflow's nodes."
)


class FakeHandle:
    def __init__(self):
        self.workflow_name = "example"
        self.workspace = "/stub"
        self.root_dir = "/stub/example"
        self.session_id = "old"
        self.completed_steps = []
        self.backend_port = 8765
        self.is_running = True
        self.main_entrypoint_path = "/stub/example/main.py"
        self.graph_plan_path = "/stub/example/workflow.json"
        self.workflow_json_path = self.graph_plan_path
        self.requirement_md_path = ""
        self.api_key = self.model = self.provider = "stub"
        self.skills_root = None
        self.actions = []
        self.steps = [
            {"id": "Entry", "inputRequired": True, "dependencies": []},
            {"id": "HeuristicGate", "inputRequired": True, "dependencies": ["Entry"]},
            # Runtime waits can exist even where static inputRequired is false.
            {"id": "ActualGate", "inputRequired": False, "dependencies": ["NotCompleted"]},
        ]
        self.builder = SimpleNamespace(
            _load_planned_graph=lambda *_args: object(),
            _build_steps_meta=lambda **_kwargs: list(self.steps),
            generate_main_entrypoint=lambda *_args, **_kwargs: self.main_entrypoint_path,
            _sync_workflow_graph_json=lambda **_kwargs: self.workflow_json_path,
        )

    def base_url(self):
        return "http://stub"

    def new_session(self):
        self.actions.append("rotate")
        self.session_id = "new"
        self.completed_steps = []
        return self.session_id

    def start_backend(self, **_kwargs):
        self.actions.append("start")
        self.is_running = True
        return {"is_running": True}

    def stop_backend(self):
        self.actions.append("stop")
        self.is_running = False

    def close(self):
        self.stop_backend()
        self.actions.append("close")

    def reload_backend(self, reset_session=True, **kwargs):
        self.stop_backend()
        if reset_session:
            self.new_session()
        return self.start_backend(**kwargs)

    def sync_artifacts(self):
        pass


@pytest.fixture
def tools(mcp_modules, monkeypatch):
    _, server = mcp_modules
    handle = FakeHandle()
    assert server._MCP_SERVER_AVAILABLE, "MCP SDK entry point must resolve to its stub"
    monkeypatch.setattr(server, "_require_handle", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(server, "health_check", lambda *_args, **_kwargs: True)
    formats = ModuleType("flowx_core.tools.node_formats")
    formats.collect_node_formats = lambda *_args: {}
    monkeypatch.setitem(sys.modules, formats.__name__, formats)
    registered = server.create_server().tools

    def call(name, **kwargs):
        return json.loads(registered[name](workflow_name="example", **kwargs))

    return call, handle, server


@pytest.fixture
def deferred_batch_tools(tools):
    call, handle, server = tools
    handle.builder.generate_nodes = Mock(side_effect=NotImplementedError(_BATCH_GENERATION_ERROR))
    handle.builder._sync_workflow_graph_json = Mock()
    handle.builder.generate_main_entrypoint = Mock()
    handle.sync_artifacts = Mock()
    return call, handle, server


def test_create_workflow_reports_deferred_batch_error_without_sync_or_main(
    deferred_batch_tools, fake_http, monkeypatch, tmp_path,
):
    call, handle, server = deferred_batch_tools
    requests, _ = fake_http
    requirement_path = "/stub/example/requirement_analysis.md"
    handle.builder.analyze_requirement = Mock(return_value=requirement_path)
    handle.builder.plan_graph = Mock(return_value=handle.graph_plan_path)
    monkeypatch.setattr(server, "core_available", lambda: True)
    monkeypatch.setattr(server, "registry", SimpleNamespace(get_or_create=Mock(return_value=handle)))

    result = call(
        "create_workflow", user_prompt="Collect input", workspace=str(tmp_path), backend_port=8765,
    )

    assert result["ok"] is False
    assert result["tool"] == "create_workflow"
    assert result["error"] == _BATCH_GENERATION_ERROR
    handle.builder.analyze_requirement.assert_called_once_with(requirement_text="Collect input")
    handle.builder.plan_graph.assert_called_once_with(
        requirement_md_path=requirement_path,
        graph_plan_filename="workflow.json",
        temperature=0.3,
    )
    handle.builder.generate_nodes.assert_called_once_with(
        graph_plan_path=handle.graph_plan_path,
        requirement_md_path=requirement_path,
        language="python",
        temperature=0.3,
        generate_markdowns=True,
        workflow_name=handle.workflow_name,
    )
    handle.builder._sync_workflow_graph_json.assert_not_called()
    handle.builder.generate_main_entrypoint.assert_not_called()
    handle.sync_artifacts.assert_not_called()
    assert requests == []


def test_update_workflow_node_reports_deferred_batch_error_without_sync_main_or_backend_reset(
    deferred_batch_tools, fake_http, monkeypatch,
):
    call, handle, server = deferred_batch_tools
    requests, _ = fake_http
    handle.completed_steps = ["Entry"]
    run_state = {"session_id": handle.session_id, "response": {"awaitingInput": "ActualGate"}}
    handle._mcp_run_state = run_state
    amended_path = "/stub/example/amended_workflow.json"
    handle.builder.amend_workflow_json = Mock(return_value=amended_path)
    reset_backend_session = Mock()
    monkeypatch.setattr(server, "reset_backend_session", reset_backend_session)

    result = call("update_workflow_node", node_name="Entry", user_prompt="Update input")

    assert result["ok"] is False
    assert result["tool"] == "update_workflow_node"
    assert result["error"] == _BATCH_GENERATION_ERROR
    handle.builder.amend_workflow_json.assert_called_once_with(
        user_prompt="Update input",
        workflow_json_path=handle.workflow_json_path,
        temperature=0.2,
    )
    handle.builder.generate_nodes.assert_called_once_with(
        requirement_md_path=None,
        graph_plan_path=amended_path,
        node_names=["Entry"],
        temperature=0.2,
        generate_markdowns=True,
        markdown_amendments={"Entry": "Update input"},
        workflow_name=handle.workflow_name,
    )
    handle.builder._sync_workflow_graph_json.assert_not_called()
    handle.builder.generate_main_entrypoint.assert_not_called()
    handle.sync_artifacts.assert_not_called()
    reset_backend_session.assert_not_called()
    assert handle.is_running is True
    assert handle.session_id == "old"
    assert handle.completed_steps == ["Entry"]
    assert handle._mcp_run_state is run_state
    assert handle.actions == []
    assert requests == []


def pause(responses, *, waiting="ActualGate", completed=None):
    responses.append([
        {"type": "CUSTOM", "name": "input_required", "value": {
            "stepId": waiting, "title": "Backend gate", "prompt": "Answer?",
        }},
        {"type": "RUN_FINISHED", "result": {
            "ok": True, "stepId": "Entry", "isFinal": False,
            "completedSteps": ["Entry"] if completed is None else completed,
            "awaitingInput": waiting,
        }},
    ])


def test_backend_waiting_step_overrides_static_order_and_dependencies(tools, fake_http):
    call, handle, _ = tools
    requests, responses = fake_http
    pause(responses)
    result = call("run_workflow_step", chat_request="start")
    assert result["next_input_step"]["id"] == "ActualGate"
    assert result["response"]["status"] == "awaiting_input"
    assert call("get_node_input_formats")["next_input_step"]["id"] == "ActualGate"

    pause(responses)
    result = call("run_workflow_step", chat_request="answer")
    assert result["selected_step"]["stepId"] == "ActualGate"
    assert json.loads(requests[-1][0].data)["stepId"] == "ActualGate"
    assert handle.completed_steps == ["Entry"]


def test_waiting_step_not_in_static_metadata_is_still_actionable(tools, fake_http):
    call, _, _ = tools
    requests, responses = fake_http
    pause(responses, waiting="DynamicGate")
    result = call("run_workflow_step", chat_request="start")
    assert result["next_input_step"]["id"] == "DynamicGate"
    assert result["next_input_step"]["prompt"] == "Answer?"
    pause(responses, waiting="DynamicGate")
    call("run_workflow_step", chat_request="answer")
    assert json.loads(requests[-1][0].data)["stepId"] == "DynamicGate"


def test_terminal_completion_does_not_offer_metadata_only_input_steps(tools, fake_http):
    call, _, _ = tools
    _, responses = fake_http
    responses.append([{"type": "RUN_FINISHED", "result": {
        "ok": True, "stepId": "Entry", "isFinal": True,
        "completedSteps": ["Entry"], "final": {"answer": 42},
    }}])
    result = call("run_workflow_step", chat_request="start")
    assert result["response"]["final"] == {"answer": 42}
    assert result["next_input_step"] is None
    assert call("get_node_input_formats")["next_input_step"] is None


def test_fully_automatic_graph_starts_once_without_advertising_user_input(tools, fake_http):
    call, handle, _ = tools
    requests, responses = fake_http
    handle.steps = [
        {"id": "Disabled", "enabled": False, "inputRequired": False},
        {"id": "Automatic", "inputRequired": False, "dependencies": []},
    ]
    assert call("get_node_input_formats")["next_input_step"] is None
    responses.append([{"type": "RUN_FINISHED", "result": {
        "ok": True, "stepId": "Automatic", "isFinal": True,
        "completedSteps": ["Automatic"], "final": {"answer": 42},
    }}])
    result = call("run_workflow_step")
    assert result["selected_step"]["stepId"] == "Automatic"
    assert result["response"]["final"] == {"answer": 42}
    assert result["next_input_step"] is None
    assert len(requests) == 1
    assert call("run_workflow_step")["message"] == "No pending user-input step was found."
    assert len(requests) == 1


@pytest.mark.parametrize("tool", ["run_workflow_step", "start_backend"])
def test_live_reset_cancels_old_backend_request_before_rotation(tools, fake_http, tool):
    call, handle, _ = tools
    requests, responses = fake_http
    handle.completed_steps = ["Entry"]
    pause(responses)
    call("run_workflow_step", step_id="Entry")
    responses.append({"ok": True, "sessionId": "old", "runId": "rotated-request"})
    if tool == "run_workflow_step":
        pause(responses)
    result = call(tool, reset_session=True)
    assert result["ok"] is True
    reset_request = requests[1][0]
    assert reset_request.full_url.endswith("/api/reset-session")
    assert json.loads(reset_request.data) == {"sessionId": "old"}
    assert handle.session_id == "new"
    assert handle.actions == ["rotate"]
    if tool == "run_workflow_step":
        assert json.loads(requests[2][0].data)["sessionId"] == "new"
        assert result["selected_step"]["stepId"] == "Entry"
    else:
        assert call("get_node_input_formats")["next_input_step"]["id"] == "Entry"


@pytest.mark.parametrize("failure", [
    {"ok": False, "error": "lease cancellation failed"},
    urllib.error.URLError("backend unreachable"),
    b"not json",
])
@pytest.mark.parametrize("tool", ["run_workflow_step", "start_backend"])
def test_reset_failure_keeps_live_session_and_progress(tools, fake_http, failure, tool):
    call, handle, _ = tools
    requests, responses = fake_http
    pause(responses)
    call("run_workflow_step")
    responses.append(failure)
    result = call(tool, reset_session=True)
    assert result["ok"] is False
    assert "reset" in result["error"].lower()
    assert handle.session_id == "old"
    assert handle.completed_steps == ["Entry"]
    assert handle.actions == []
    assert len(requests) == 2
    assert call("get_node_input_formats")["next_input_step"]["id"] == "ActualGate"


@pytest.mark.parametrize("tool", ["start_backend", "reload_workflow", "run_workflow_step"])
def test_backend_restart_without_session_rotation_clears_stale_progress(tools, fake_http, tool):
    call, handle, _ = tools
    _, responses = fake_http
    pause(responses)
    call("run_workflow_step")
    handle.is_running = False
    if tool == "run_workflow_step":
        pause(responses, completed=[])
    result = call(tool, reset_session=False)
    assert result["ok"] is True
    assert handle.session_id == "old"
    assert handle.completed_steps == []
    if tool == "run_workflow_step":
        assert result["selected_step"]["stepId"] == "Entry"
    else:
        assert call("get_node_input_formats")["next_input_step"]["id"] == "Entry"


def test_restart_builder_does_not_restore_progress_from_dead_native_run(tools, monkeypatch):
    _, previous, server = tools
    previous.completed_steps = ["Entry"]
    replacement = FakeHandle()
    monkeypatch.setattr(server, "registry", SimpleNamespace(pop=lambda *_args, **_kwargs: previous))
    monkeypatch.setattr(server, "_attach_existing_workflow", lambda *_args, **_kwargs: replacement)
    handle, was_running = server._restart_builder_handle("example", reset_session=False)
    assert was_running is True
    assert previous.actions == ["stop", "close"]
    assert handle.session_id == "old"
    assert handle.completed_steps == []


def test_disabled_nodes_are_not_selected_or_advertised(tools, fake_http):
    call, handle, server = tools
    requests, responses = fake_http
    handle.steps.insert(0, {
        "id": "Disabled", "enabled": False, "inputRequired": True, "dependencies": [],
    })
    assert server._find_step_meta(handle, "Disabled") is None
    summary = call("get_node_input_formats")
    assert "Disabled" not in {node["stepId"] for node in summary["input_nodes"]}
    assert summary["next_input_step"]["id"] == "Entry"
    result = call("run_workflow_step", step_id="Disabled")
    assert result["ok"] is False
    assert requests == []
    pause(responses)
    result = call("run_workflow_step")
    assert result["selected_step"]["stepId"] == "Entry"


def test_explicit_step_selection_still_overrides_waiting_step(tools, fake_http):
    call, _, _ = tools
    requests, responses = fake_http
    pause(responses)
    call("run_workflow_step")
    pause(responses)
    result = call("run_workflow_step", step_id="HeuristicGate", input_json={"text": "explicit"})
    assert result["selected_step"]["stepId"] == "HeuristicGate"
    assert json.loads(requests[-1][0].data)["input"] == {"text": "explicit"}
    assert result["next_input_step"]["id"] == "ActualGate"


def test_http_reset_is_confirmed_before_new_session_is_called(tools, fake_http, monkeypatch):
    call, handle, _ = tools
    requests, responses = fake_http
    rotate = handle.new_session

    def checked_rotation():
        assert len(requests) == 1
        assert requests[0][0].full_url.endswith("/api/reset-session")
        assert json.loads(requests[0][0].data)["sessionId"] == "old"
        assert len(responses) == 1, "Reset response must be consumed before rotation"
        return rotate()

    monkeypatch.setattr(handle, "new_session", checked_rotation)
    responses.append({"ok": True, "sessionId": "old"})
    pause(responses)
    assert call("run_workflow_step", reset_session=True)["ok"] is True


@pytest.mark.parametrize("tool", ["run_workflow_step", "start_backend"])
def test_unhealthy_live_backend_reset_does_not_rotate_or_autostart(tools, fake_http, monkeypatch, tool):
    call, handle, server = tools
    requests, responses = fake_http
    handle.completed_steps = ["Entry"]
    monkeypatch.setattr(server, "health_check", lambda *_args, **_kwargs: False)
    responses.append(urllib.error.URLError("unreachable"))
    result = call(tool, reset_session=True)
    assert result["ok"] is False
    assert handle.session_id == "old"
    assert handle.completed_steps == ["Entry"]
    assert handle.actions == []
    assert len(requests) == 1


@pytest.mark.parametrize("tool", ["start_backend", "run_workflow_step"])
def test_health_triggered_backend_restart_clears_progress(tools, fake_http, monkeypatch, tool):
    call, handle, server = tools
    _, responses = fake_http
    pause(responses)
    call("run_workflow_step")
    monkeypatch.setattr(server, "health_check", lambda *_args, **_kwargs: False)
    if tool == "run_workflow_step":
        pause(responses, completed=[])
    result = call(tool)
    assert result["ok"] is True
    assert handle.actions == ["start"]
    assert handle.completed_steps == []
    if tool == "run_workflow_step":
        assert result["selected_step"]["stepId"] == "Entry"
    else:
        assert call("get_node_input_formats")["next_input_step"]["id"] == "Entry"


def test_session_change_cannot_reuse_old_waiting_or_completion(tools, fake_http):
    call, handle, _ = tools
    _, responses = fake_http
    pause(responses)
    call("run_workflow_step")
    handle.new_session()
    assert call("get_node_input_formats")["next_input_step"]["id"] == "Entry"


def test_disabled_backend_wait_is_reported_as_inconsistent_not_complete(tools, fake_http):
    call, handle, _ = tools
    _, responses = fake_http
    handle.steps.append({"id": "Disabled", "enabled": False, "inputRequired": True})
    pause(responses, waiting="Disabled")
    result = call("run_workflow_step")
    assert result["ok"] is False
    assert "disabled" in result["error"]


def test_failure_is_not_advertised_as_completion(tools, fake_http):
    call, _, _ = tools
    _, responses = fake_http
    responses.append([
        {"type": "RUN_ERROR", "message": "processing failed"},
        {"type": "RUN_FINISHED", "result": {"ok": False, "stepId": "Entry"}},
    ])
    result = call("run_workflow_step")
    assert result["ok"] is False
    assert result["response"]["status"] == "failed"
    assert result["response"]["isFinal"] is False
    assert result["response"]["error"] == "processing failed"