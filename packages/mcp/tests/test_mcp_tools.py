# SPDX-License-Identifier: Apache-2.0
"""MCP tools delegate to the FlowX engine API and report results and failures as JSON."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def call(mcp_server, name, **kwargs):
    return json.loads(mcp_server.tools[name](**kwargs))


class _Service(BaseHTTPRequestHandler):
    """Stand-in for the generated main entrypoint: records requests, replays one response."""

    requests: list = []
    response = b""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append({"path": self.path, "json": json.loads(body or b"{}")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(type(self).response)


@pytest.fixture
def local_service(mcp_server):
    _Service.requests, _Service.response = [], b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    mcp_server.module._SERVICES["wf1"] = base_url
    yield base_url
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_registers_one_tool_per_engine_function(mcp_server):
    assert set(mcp_server.tools) == {
        "build_workflow_json", "build_nodes", "build_main_entrypoint", "build_workflow", "evolve_workflow",
        "run_service", "stop_service", "list_workflows", "list_nodes", "get_workflow",
        "run_step", "reset_session",
    }


def test_build_workflow_json_creates_or_modifies(mcp_server):
    mcp_server.client.results["build_workflow_json"] = {"ok": True, "workflow_id": "wf1", "stages": {}}

    created = call(mcp_server, "build_workflow_json", prompt="  Collect tickets  ")
    modified = call(mcp_server, "build_workflow_json", prompt="Add a priority node", workflow_id="wf1", temperature=0.1)

    assert created["workflow_id"] == "wf1"
    assert modified["ok"] is True
    assert mcp_server.client.calls == [
        ("build_workflow_json", ("Collect tickets",), {"workflow_id": None, "temperature": 0.35}),
        ("build_workflow_json", ("Add a priority node",), {"workflow_id": "wf1", "temperature": 0.1}),
    ]


@pytest.mark.parametrize("tool, kwargs", [
    ("build_workflow_json", {"prompt": "   "}),
    ("build_workflow", {}),
    ("build_workflow", {"prompt": "  "}),
])
def test_empty_requests_are_rejected_without_calling_the_engine(mcp_server, tool, kwargs):
    result = call(mcp_server, tool, **kwargs)

    assert result["ok"] is False
    assert result["tool"] == tool
    assert mcp_server.client.calls == []


def test_build_nodes_passes_specific_node_amendments(mcp_server):
    call(mcp_server, "build_nodes", workflow_id="wf1")
    call(mcp_server, "build_nodes", workflow_id="wf1", amendments={"Reply": "Add an escalation note"}, cascade=True)

    assert mcp_server.client.calls == [
        ("build_nodes", ("wf1", None), {"cascade": False}),
        ("build_nodes", ("wf1", {"Reply": "Add an escalation note"}), {"cascade": True}),
    ]


def test_build_main_entrypoint_passes_port_and_smoke_test(mcp_server):
    call(mcp_server, "build_main_entrypoint", workflow_id="wf1", amendment="Use port 9000", fastapi_port=9000, smoke_test=True)

    assert mcp_server.client.calls == [
        ("build_main_entrypoint", ("wf1", "Use port 9000"), {"fastapi_port": 9000, "smoke_test_main": True}),
    ]


def test_build_workflow_accepts_logs_without_a_prompt(mcp_server):
    call(mcp_server, "build_workflow", logs=["Traceback in Reply"], workflow_id="wf1", fix_errors=True, serve=True)
    call(mcp_server, "build_workflow", prompt="Build a ticket triage workflow")

    first, second = mcp_server.client.calls
    assert first == ("build_workflow", ("",), {
        "workflow_id": "wf1", "logs": ("Traceback in Reply",), "fix_errors": True, "cascade": True,
        "fastapi_port": 8000, "smoke_test_main": False, "serve": True,
    })
    assert second[1] == ("Build a ticket triage workflow",)
    assert second[2]["workflow_id"] is None and second[2]["logs"] == ()


def test_evolve_workflow_passes_the_target_or_leaves_it_to_the_engine(mcp_server):
    call(mcp_server, "evolve_workflow", workflow_id="wf1", command="Add a summary", target="workflow")
    call(mcp_server, "evolve_workflow", workflow_id="wf1", command="Escalate urgent tickets", target="Reply", serve=True)
    call(mcp_server, "evolve_workflow", workflow_id="wf1", command="Be brief", cascade=False)

    options = {"cascade": True, "fastapi_port": 8000, "smoke_test_main": False, "serve": False}
    assert mcp_server.client.calls == [
        ("evolve", ("wf1", "Add a summary"), {**options, "target": "workflow"}),
        ("evolve", ("wf1", "Escalate urgent tickets"), {**options, "target": "Reply", "serve": True}),
        ("evolve", ("wf1", "Be brief"), {**options, "target": None, "cascade": False}),
    ]


def test_evolve_workflow_rejects_an_empty_command(mcp_server):
    result = call(mcp_server, "evolve_workflow", workflow_id="wf1", command="  ")

    assert result["ok"] is False and result["tool"] == "evolve_workflow"
    assert mcp_server.client.calls == []


def test_list_nodes_returns_step_and_node_ids(mcp_server):
    mcp_server.client.results["list_nodes"] = [{"step_id": "Reply", "node_id": "n1", "version": 1}]

    assert call(mcp_server, "list_nodes", workflow_id="wf1") == {
        "ok": True, "nodes": [{"step_id": "Reply", "node_id": "n1", "version": 1}],
    }
    assert mcp_server.client.calls == [("list_nodes", ("wf1",), {})]


def test_service_and_inspection_tools(mcp_server):
    client = mcp_server.client
    client.results.update({
        "run_service": {"pid": 42, "cwd": "/tmp/wf1"},
        "workflows": {"wf1": {"has_main": True}},
        "artifacts": {"workflow_id": "wf1", "graph": Path("/tmp/wf1/workflow.json")},
    })

    assert call(mcp_server, "run_service", workflow_id="wf1") == {"ok": True, "service": {"pid": 42, "cwd": "/tmp/wf1"}}
    assert call(mcp_server, "stop_service", workflow_id="wf1") == {"ok": True, "workflow_id": "wf1"}
    assert call(mcp_server, "list_workflows") == {"ok": True, "workflows": {"wf1": {"has_main": True}}}
    # Paths in engine artifacts are serialized as strings.
    assert call(mcp_server, "get_workflow", workflow_id="wf1")["workflow"]["graph"] == "/tmp/wf1/workflow.json"
    assert [name for name, *_ in client.calls] == ["run_service", "stop_service", "workflows", "artifacts"]


def test_engine_errors_become_tool_errors(mcp_server):
    mcp_server.client.results["run_service"] = FileNotFoundError("workflow wf1 has no main entrypoint; build it first")

    result = call(mcp_server, "run_service", workflow_id="wf1")

    assert result["ok"] is False
    assert result["tool"] == "run_service"
    assert "has no main entrypoint" in result["error"]


def test_builder_output_does_not_reach_stdout(mcp_server, capsys):
    def noisy(*_args, **_kwargs):
        print("builder progress")
        return {"ok": True}

    mcp_server.client.build_nodes = noisy

    assert call(mcp_server, "build_nodes", workflow_id="wf1") == {"ok": True}
    assert capsys.readouterr().out == ""


def test_run_server_closes_the_client_so_services_stop(mcp_server, monkeypatch):
    module = mcp_server.module
    ran = []
    monkeypatch.setattr(module, "create_server", lambda: type("S", (), {"run": lambda self: ran.append(True)})())
    module._get_client()

    module.run_server()

    assert ran == [True]
    assert mcp_server.client.closed is True
    assert module._client is None


def test_run_service_caches_the_base_url_for_the_step_proxy(mcp_server):
    mcp_server.client.results["run_service"] = {"pid": 42, "base_url": "http://127.0.0.1:8123"}

    result = call(mcp_server, "run_service", workflow_id="wf1")
    assert result["service"]["base_url"] == "http://127.0.0.1:8123"
    assert mcp_server.module._SERVICES == {"wf1": "http://127.0.0.1:8123"}

    call(mcp_server, "stop_service", workflow_id="wf1")
    assert mcp_server.module._SERVICES == {}


def test_run_step_reports_the_step_that_wants_input(mcp_server, local_service):
    _Service.response = (
        b'data: {"type": "CUSTOM", "name": "input_required", '
        b'"value": {"stepId": "ask", "title": "Ask", "prompt": "Name?"}}\n\n'
        b'data: {"type": "RUN_FINISHED", "result": {"ok": true, "awaitingInput": "ask"}}\n\n'
    )

    result = call(mcp_server, "run_step", workflow_id="wf1", step_id="prepare")

    assert result["status"] == "input_required"
    assert result["awaiting"] == {"stepId": "ask", "title": "Ask", "prompt": "Name?"}
    assert result["final"] == {"ok": True, "awaitingInput": "ask"}
    assert result["session_id"] == mcp_server.module._SESSIONS["wf1"]
    assert _Service.requests == [{
        "path": "/api/run-step",
        "json": {"sessionId": result["session_id"], "stepId": "prepare", "input": None},
    }]


def test_run_step_resumes_the_session_and_reset_rotates_the_run(mcp_server, local_service):
    _Service.response = b'data: {"type": "RUN_FINISHED", "result": {"ok": true, "isFinal": true}}\n\n'

    answered = call(mcp_server, "run_step", workflow_id="wf1", step_id="ask", input="Ada", session_id="s1")
    _Service.response = b'{"ok": true, "sessionId": "s1", "threadId": "t1", "runId": "r2"}'
    reset = call(mcp_server, "reset_session", workflow_id="wf1", session_id="s1")

    assert answered["status"] == "completed"
    assert _Service.requests[0] == {
        "path": "/api/run-step", "json": {"sessionId": "s1", "stepId": "ask", "input": "Ada"},
    }
    assert reset["reset"] == {"ok": True, "sessionId": "s1", "threadId": "t1", "runId": "r2"}
    assert _Service.requests[1] == {"path": "/api/reset-session", "json": {"sessionId": "s1"}}


def test_run_step_requires_a_running_service(mcp_server):
    mcp_server.module._SERVICES.clear()

    result = call(mcp_server, "run_step", workflow_id="wf1", step_id="ask")

    assert result["ok"] is False
    assert "call run_service first" in result["error"]
