# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-03: test background build polling and guarded repair recovery.
# Modified 2026-11-03: migrate synchronous build coverage to background-only MCP tools.
"""MCP tools delegate to the FlowX engine API and report results and failures as JSON."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


def call(mcp_server, name, **kwargs):
    return json.loads(mcp_server.tools[name](**kwargs))


def engine_calls(client):
    """Recorded engine calls, without the pending-build probe every build starts with."""
    return [entry for entry in client.calls if entry[0] != "pending_requests"]


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
        "list_pending_builds", "create_workflow", "start_new_workflow", "start_build_workflow_json",
        "start_build_main_entrypoint", "start_build_nodes", "start_build_workflow",
        "start_evolve_workflow", "get_build_status", "submit_background_build_input", "abort_background_build",
        "recover_background_build",
        "run_service", "stop_service", "list_workflows", "list_nodes", "get_workflow",
        "run_step", "reset_session",
    }


START_CASES = [
    ("start_new_workflow", {"workflow_id": "wf1", "prompt": "create"}),
    ("start_build_workflow_json", {"workflow_id": "wf1", "prompt": "change graph"}),
    ("start_build_main_entrypoint", {"workflow_id": "wf1"}),
    ("start_build_nodes", {"workflow_id": "wf1"}),
    ("start_build_workflow", {"workflow_id": "wf1", "prompt": "change"}),
    ("start_evolve_workflow", {"workflow_id": "wf1", "command": "evolve"}),
]


@pytest.mark.parametrize("tool, kwargs, method", [
    (*START_CASES[0], "build_new_workflow"),
    (*START_CASES[1], "build_allocated_workflow_json"),
    (*START_CASES[2], "build_main_entrypoint"),
    (*START_CASES[3], "build_nodes"),
    (*START_CASES[4], "build_workflow"),
    (*START_CASES[5], "evolve"),
])
def test_each_start_returns_before_sdk_finishes(mcp_server, monkeypatch, tool, kwargs, method):
    started, release = threading.Event(), threading.Event()

    def build(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return {"ok": True, "workflow_id": "wf1"}

    monkeypatch.setattr(mcp_server.client, method, build)
    try:
        accepted = call(mcp_server, tool, **kwargs)
        assert accepted["status"] == "running" and accepted["action_required"] == "get_build_status"
        assert started.wait(5)
        assert call(mcp_server, "get_build_status", workflow_id="wf1")["status"] == "running"
    finally:
        release.set()
        if started.is_set():
            assert _wait_for_status(mcp_server, "wf1", "completed")["result"]["ok"] is True


@pytest.mark.parametrize("tool, kwargs", [
    ("start_new_workflow", {"workflow_id": "wf1", "prompt": "  "}),
    ("start_build_workflow_json", {"workflow_id": "wf1", "prompt": "  "}),
    ("start_build_workflow", {"workflow_id": "wf1"}),
    ("start_build_workflow", {"workflow_id": "wf1", "prompt": "  "}),
    ("start_evolve_workflow", {"workflow_id": "wf1", "command": "  "}),
])
def test_empty_requests_are_rejected_without_calling_the_engine(mcp_server, tool, kwargs):
    result = call(mcp_server, tool, **kwargs)
    assert result["ok"] is False and result["tool"] == tool
    assert mcp_server.client.calls == []


@pytest.mark.parametrize("tool, kwargs", START_CASES)
def test_start_requires_nonempty_workflow_id(mcp_server, tool, kwargs):
    result = call(mcp_server, tool, **{**kwargs, "workflow_id": "  "})
    assert result["ok"] is False and result["tool"] == tool
    assert mcp_server.client.calls == []


@pytest.mark.parametrize("tool, kwargs", START_CASES)
def test_start_requires_workflow_id_argument(mcp_server, tool, kwargs):
    with pytest.raises(TypeError):
        call(mcp_server, tool, **{key: value for key, value in kwargs.items() if key != "workflow_id"})
    assert mcp_server.client.calls == []


@pytest.mark.parametrize("prompt, temperature", [("  Collect tickets  ", 0.35), ("Add a priority node", 0.1)])
def test_start_graph_creates_or_modifies_allocated_workflow(mcp_server, prompt, temperature):
    kwargs = {"workflow_id": "wf1", "prompt": prompt}
    if temperature != 0.35:
        kwargs["temperature"] = temperature
    started = call(mcp_server, "start_build_workflow_json", **kwargs)
    assert started["workflow_id"] == "wf1" and started["status"] == "running"
    result = _wait_for_status(mcp_server, "wf1", "completed")
    assert result["result"]["ok"] is True
    assert engine_calls(mcp_server.client) == [
        ("build_allocated_workflow_json", ("wf1", prompt.strip()), {"temperature": temperature}),
    ]


@pytest.mark.parametrize("kwargs, expected", [
    ({}, ("", 8000, False)),
    ({"amendment": "Use port 9000", "fastapi_port": 9000, "smoke_test": True},
     ("Use port 9000", 9000, True)),
])
def test_start_main_entrypoint_forwards_options(mcp_server, kwargs, expected):
    started = call(mcp_server, "start_build_main_entrypoint", workflow_id="wf1", **kwargs)
    assert started["status"] == "running"
    assert _wait_for_status(mcp_server, "wf1", "completed")["result"]["ok"] is True
    amendment, port, smoke_test = expected
    assert engine_calls(mcp_server.client) == [
        ("build_main_entrypoint", ("wf1", amendment), {"fastapi_port": port, "smoke_test_main": smoke_test}),
    ]


@pytest.mark.parametrize("kwargs, expected", [
    ({}, (None, False, True)),
    ({"amendments": {"Reply": "Add an escalation note"}, "cascade": True, "interactive": False},
     ({"Reply": "Add an escalation note"}, True, False)),
])
def test_start_nodes_forwards_amendments_and_interactive(mcp_server, kwargs, expected):
    assert call(mcp_server, "start_build_nodes", workflow_id="wf1", **kwargs)["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    amendments, cascade, interactive = expected
    assert engine_calls(mcp_server.client) == [
        ("build_nodes", ("wf1", amendments), {"cascade": cascade, "await_repair_input": interactive}),
    ]


@pytest.mark.parametrize("target, cascade, serve", [("workflow", True, False), ("Reply", True, True), (None, False, False)])
def test_start_evolve_forwards_target_and_options(mcp_server, target, cascade, serve):
    started = call(mcp_server, "start_evolve_workflow", workflow_id="wf1", command="Add a summary",
                   target=target, cascade=cascade, fastapi_port=9000, smoke_test=True,
                   serve=serve, interactive=False)
    assert started["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    assert engine_calls(mcp_server.client) == [("evolve", ("wf1", "Add a summary"), {
        "target": target, "cascade": cascade, "fastapi_port": 9000, "smoke_test_main": True,
        "serve": serve, "await_repair_input": False,
    })]


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
    assert call(mcp_server, "start_build_nodes", workflow_id="wf1")["status"] == "running"
    assert _wait_for_status(mcp_server, "wf1", "completed")["result"] == {"ok": True}
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


def _wait_for_status(mcp_server, workflow_id, *statuses):
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = call(mcp_server, "get_build_status", workflow_id=workflow_id)
        if result.get("status") in statuses:
            return result
        threading.Event().wait(0.005)
    pytest.fail(f"build did not reach {statuses}: {result}")


def test_list_pending_builds_is_registry_only_when_idle(mcp_server):
    mcp_server.client.results["pending_requests"] = RuntimeError("must not probe SDK")
    result = call(mcp_server, "list_pending_builds")
    assert result["ok"] is True and result["count"] == 0 and result["pending"] == []
    assert result.get("action_required") is None
    assert result.get("active_workflow_id") is None
    assert mcp_server.client.calls == []


@pytest.mark.parametrize("tool, kwargs", START_CASES)
def test_start_is_busy_while_another_build_runs(mcp_server, tool, kwargs):
    started, release = threading.Event(), threading.Event()

    def build(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return {"ok": True, "workflow_id": "active"}

    mcp_server.client.build_nodes = build
    accepted = call(mcp_server, "start_build_nodes", workflow_id="active")
    try:
        assert accepted["status"] == "running" and started.wait(5)
        calls_before = list(mcp_server.client.calls)
        pending = call(mcp_server, "list_pending_builds")
        assert pending["ok"] is True and pending["count"] == 0 and pending["pending"] == []
        assert pending["active_workflow_id"] == "active"
        assert mcp_server.client.calls == calls_before
        busy = call(mcp_server, tool, **kwargs)
        assert busy["ok"] is False and busy["tool"] == tool
        assert "active" in busy["error"]
        assert not call(mcp_server, "create_workflow")["ok"]
        assert mcp_server.client.calls == calls_before
    finally:
        release.set()
        if started.is_set():
            _wait_for_status(mcp_server, "active", "completed")


@pytest.mark.parametrize("tool, kwargs", START_CASES)
def test_orphan_sdk_pause_refuses_background_build_without_removed_tool_guidance(mcp_server, tool, kwargs):
    mcp_server.client.results["pending_requests"] = [{
        "request_id": "orphan", "workflow_id": "other",
        "awaiting": {"stepId": "Reply__amend_node", "node": "Reply"},
    }]
    accepted = call(mcp_server, tool, **kwargs)
    assert accepted["status"] == "running"
    failed = _wait_for_status(mcp_server, "wf1", "failed")
    assert failed["error"]
    assert "submit_build_input" not in json.dumps(failed)
    assert "abort_build" not in json.dumps(failed)
    assert engine_calls(mcp_server.client) == []
    assert mcp_server.client.calls == [("pending_requests", (), {})]
    calls_before = list(mcp_server.client.calls)
    pending = call(mcp_server, "list_pending_builds")
    assert pending["count"] == 0 and pending["pending"] == []
    assert mcp_server.client.calls == calls_before


@pytest.mark.parametrize("tool, kwargs", START_CASES)
def test_pending_probe_errors_are_reported_by_status(mcp_server, capsys, tool, kwargs):
    mcp_server.client.results["pending_requests"] = RuntimeError("pending probe failed")
    accepted = call(mcp_server, tool, **kwargs)
    assert accepted["status"] == "running"
    failed = _wait_for_status(mcp_server, "wf1", "failed")
    assert "pending probe failed" in failed["error"]
    assert mcp_server.client.calls == [("pending_requests", (), {})]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("pending", [
    RuntimeError("pending probe failed"),
    [{"request_id": "orphan", "workflow_id": "other", "awaiting": {"stepId": "Reply__amend_node"}}],
])
def test_create_workflow_rejects_pending_probe_failure_or_orphan(mcp_server, pending):
    mcp_server.client.results["pending_requests"] = pending
    result = call(mcp_server, "create_workflow")
    assert result["ok"] is False and result["error"]
    assert "submit_build_input" not in result["error"] and "abort_build" not in result["error"]
    assert mcp_server.client.calls == [("pending_requests", (), {})]


def test_background_build_poll_resume_and_stale_answers(mcp_server):
    started, release = threading.Event(), threading.Event()
    paused = {"ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r1",
              "awaiting": {"stepId": "Reply__amend_node", "node": "Reply", "test_log": "boom"}}

    def build(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return paused

    mcp_server.client.build_nodes = build
    mcp_server.client.results["answer"] = {"ok": True, "workflow_id": "wf1", "status": "completed"}
    accepted = call(mcp_server, "start_build_nodes", workflow_id="wf1")
    try:
        assert accepted["status"] == "running" and accepted["revision"] == 1
        assert started.wait(5)
        assert call(mcp_server, "get_build_status", workflow_id="wf1")["status"] == "running"
        assert not call(mcp_server, "start_build_nodes", workflow_id="wf2")["ok"]
    finally:
        release.set()
    question = _wait_for_status(mcp_server, "wf1", "input_required")
    assert question["request_id"] == "r1" and question["awaiting"]["test_log"] == "boom"
    assert question["action_required"] == "submit_background_build_input"
    calls_before = list(mcp_server.client.calls)
    pending = call(mcp_server, "list_pending_builds")
    assert pending["ok"] is True and pending["count"] == 1
    assert pending["action_required"] == "submit_background_build_input"
    assert pending["pending"] == [{"workflow_id": "wf1", "revision": question["revision"],
                                   "request_id": "r1", "awaiting": question["awaiting"]}]
    assert mcp_server.client.calls == calls_before
    pending["pending"][0]["awaiting"]["test_log"] = "client mutation"
    assert call(mcp_server, "get_build_status", workflow_id="wf1")["awaiting"]["test_log"] == "boom"
    assert not call(mcp_server, "start_build_nodes", workflow_id="wf2")["ok"]
    assert not call(mcp_server, "submit_background_build_input", workflow_id="wf1",
                    revision=question["revision"], request_id="r1", step_id="wrong")["ok"]
    assert not call(mcp_server, "submit_background_build_input", workflow_id="wf1",
                    revision=question["revision"], request_id="wrong", step_id="Reply__amend_node")["ok"]
    assert not call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=1,
                    request_id="r1", step_id="Reply__amend_node")["ok"]
    answer = call(mcp_server, "submit_background_build_input", workflow_id="wf1",
                  revision=question["revision"], request_id="r1", step_id="Reply__amend_node", input="skip")
    assert answer["status"] == "running"
    assert not call(mcp_server, "submit_background_build_input", workflow_id="wf1",
                    revision=question["revision"], request_id="r1", step_id="Reply__amend_node")["ok"]
    completed = _wait_for_status(mcp_server, "wf1", "completed")
    assert completed["result"]["ok"] is True
    assert ("answer", ("r1", "skip", "Reply__amend_node"), {}) in mcp_server.client.calls
    calls_before = list(mcp_server.client.calls)
    assert call(mcp_server, "list_pending_builds")["pending"] == []
    assert mcp_server.client.calls == calls_before


def test_background_repair_requires_guard_arguments(mcp_server):
    with pytest.raises(TypeError):
        call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=1, step_id="Reply")
    with pytest.raises(TypeError):
        call(mcp_server, "abort_background_build", workflow_id="wf1", revision=1)
    assert not call(mcp_server, "submit_background_build_input", workflow_id="missing", revision=1,
                    request_id="r1", step_id="Reply")["ok"]
    assert not call(mcp_server, "abort_background_build", workflow_id="missing", revision=1, request_id="r1")["ok"]
    assert mcp_server.client.calls == []


def test_background_resume_restores_pending_question_on_error(mcp_server):
    mcp_server.client.results["build_nodes"] = {
        "ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r1",
        "awaiting": {"stepId": "Reply__amend_node", "test_log": "boom"},
    }
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    question = _wait_for_status(mcp_server, "wf1", "input_required")
    mcp_server.client.results["answer"] = RuntimeError("resume failed")
    mcp_server.client.results["pending_requests"] = [{"request_id": "r1", "workflow_id": "wf1",
                                                     "awaiting": question["awaiting"]}]
    call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=question["revision"],
         request_id="r1", step_id="Reply__amend_node")
    recovered = _wait_for_status(mcp_server, "wf1", "input_required")
    assert recovered["revision"] > question["revision"] and "resume failed" in recovered["error"]
    assert not call(mcp_server, "abort_background_build", workflow_id="wf1", revision=question["revision"],
                    request_id="r1")["ok"]
    assert not call(mcp_server, "abort_background_build", workflow_id="wf1", revision=recovered["revision"],
                    request_id="wrong")["ok"]
    aborted = call(mcp_server, "abort_background_build", workflow_id="wf1", revision=recovered["revision"],
                   request_id="r1")
    assert aborted["status"] == "aborted"
    assert ("abort", ("r1",), {}) in mcp_server.client.calls
    calls_before = list(mcp_server.client.calls)
    assert call(mcp_server, "list_pending_builds")["pending"] == []
    assert mcp_server.client.calls == calls_before


def test_background_answers_can_pause_again_and_then_serialize_result(mcp_server, monkeypatch, capsys):
    first = {"ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r1",
             "awaiting": {"stepId": "Reply__amend_node", "node": "Reply", "test_log": "boom"}}
    second = {"ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r2",
              "awaiting": {"stepId": "Review__amend_node", "node": "Review", "test_log": "failed"}}
    mcp_server.client.results["build_nodes"] = first
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    question = _wait_for_status(mcp_server, "wf1", "input_required")
    mcp_server.client.results["answer"] = second
    accepted = call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=question["revision"],
                    request_id="r1", step_id="Reply__amend_node", input="use a fallback")
    assert accepted["status"] == "running"
    next_question = _wait_for_status(mcp_server, "wf1", "input_required")
    assert next_question["revision"] > question["revision"]
    assert next_question["request_id"] == "r2" and next_question["awaiting"] == second["awaiting"]
    assert next_question["action_required"] == "submit_background_build_input"
    assert "action_required" not in second

    sdk_result = object()
    mcp_server.client.results["answer"] = sdk_result
    converted = []

    def convert(result):
        converted.append(result)
        print("conversion progress")
        return {"ok": True, "workflow_id": "wf1", "status": "completed",
                "artifacts": {"graph": Path("/tmp/wf1/workflow.json")}}

    monkeypatch.setattr(mcp_server.module._sdk(), "result_to_dict", convert)
    assert call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=next_question["revision"],
                request_id="r2", step_id="Review__amend_node", input="test: update fixture")["status"] == "running"
    completed = _wait_for_status(mcp_server, "wf1", "completed")
    assert completed["result"]["artifacts"]["graph"] == "/tmp/wf1/workflow.json"
    assert converted == [sdk_result] and capsys.readouterr().out == ""
    assert engine_calls(mcp_server.client)[-2:] == [
        ("answer", ("r1", "use a fallback", "Reply__amend_node"), {}),
        ("answer", ("r2", "test: update fixture", "Review__amend_node"), {}),
    ]


@pytest.mark.parametrize("outcome", [
    {"ok": False, "workflow_id": "wf1", "status": "completed", "errors": {"nodes": "node tests failed"}},
    LookupError("no paused build for request r1"),
])
def test_background_answer_failure_is_visible_from_status(mcp_server, outcome):
    mcp_server.client.results["build_nodes"] = {
        "ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r1",
        "awaiting": {"stepId": "Reply__amend_node"},
    }
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    question = _wait_for_status(mcp_server, "wf1", "input_required")
    mcp_server.client.results["answer"] = outcome
    assert call(mcp_server, "submit_background_build_input", workflow_id="wf1", revision=question["revision"],
                request_id="r1", step_id="Reply__amend_node")["status"] == "running"
    failed = _wait_for_status(mcp_server, "wf1", "failed")
    if isinstance(outcome, Exception):
        assert "no paused build for request r1" in failed["error"]
    else:
        assert failed["result"] == outcome
    assert ("answer", ("r1", "", "Reply__amend_node"), {}) in mcp_server.client.calls


@pytest.mark.parametrize("source", ["engine", "conversion", "serialization"])
def test_background_errors_and_serialization_are_visible_from_status(mcp_server, monkeypatch, source):
    if source == "engine":
        mcp_server.client.results["build_main_entrypoint"] = ValueError("engine failed")
        message = "engine failed"
    elif source == "conversion":
        def fail_conversion(_result):
            raise ValueError("conversion failed")
        monkeypatch.setattr(mcp_server.module._sdk(), "result_to_dict", fail_conversion)
        message = "conversion failed"
    else:
        mcp_server.client.results["build_main_entrypoint"] = {"ok": True, "unsupported": object()}
        message = "not JSON serializable"
    assert call(mcp_server, "start_build_main_entrypoint", workflow_id="wf1")["status"] == "running"
    failed = _wait_for_status(mcp_server, "wf1", "failed")
    assert message in failed["error"] and failed["result"] is None
    assert call(mcp_server, "list_pending_builds")["pending"] == []


def test_background_build_error_and_new_workflow_allocation(mcp_server):
    mcp_server.client.results["create_workflow"] = "allocated"
    allocated = call(mcp_server, "create_workflow")
    assert allocated["workflow_id"] == "allocated" and allocated["action_required"] is None
    assert allocated["next_actions"] == ["start_new_workflow", "start_build_workflow_json"]
    mcp_server.client.results["build_new_workflow"] = {"ok": False, "errors": {"graph": "bad"}}
    started = call(mcp_server, "start_new_workflow", workflow_id="allocated", prompt="build it", cascade=False,
                   fastapi_port=9000, smoke_test=True, serve=True, interactive=False)
    assert started["workflow_id"] == "allocated" and started["status"] == "running"
    failed = _wait_for_status(mcp_server, "allocated", "failed")
    assert failed["result"]["errors"] == {"graph": "bad"}
    assert engine_calls(mcp_server.client) == [
        ("create_workflow", (), {}),
        ("build_new_workflow", ("allocated", "build it"), {
            "cascade": False, "fastapi_port": 9000, "smoke_test_main": True,
            "serve": True, "await_repair_input": False,
        }),
    ]
    assert not call(mcp_server, "get_build_status", workflow_id="unknown")["ok"]


def test_background_new_workflow_forwards_default_options(mcp_server):
    assert call(mcp_server, "start_new_workflow", workflow_id="wf1", prompt="build it")["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    assert engine_calls(mcp_server.client) == [("build_new_workflow", ("wf1", "build it"), {
        "cascade": True, "fastapi_port": 8000, "smoke_test_main": False,
        "serve": False, "await_repair_input": True,
    })]


def test_background_existing_workflow_forwards_options(mcp_server):
    accepted = call(mcp_server, "start_build_workflow", workflow_id="wf1", logs=["Traceback in Reply"],
                    fix_errors=True, cascade=False, fastapi_port=9000, smoke_test=True, serve=True, interactive=False)
    assert accepted["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    assert ("build_workflow", ("",), {"workflow_id": "wf1", "logs": ("Traceback in Reply",), "fix_errors": True,
        "cascade": False, "fastapi_port": 9000, "smoke_test_main": True, "serve": True,
        "await_repair_input": False}) in mcp_server.client.calls


def test_background_existing_workflow_forwards_default_options(mcp_server):
    assert call(mcp_server, "start_build_workflow", workflow_id="wf1", prompt="change it")["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    assert engine_calls(mcp_server.client) == [("build_workflow", ("change it",), {
        "workflow_id": "wf1", "logs": (), "fix_errors": False, "cascade": True,
        "fastapi_port": 8000, "smoke_test_main": False, "serve": False, "await_repair_input": True,
    })]


def test_background_build_workflow_accepts_fix_errors_without_prompt_or_logs(mcp_server):
    assert call(mcp_server, "start_build_workflow", workflow_id="wf1", fix_errors=True)["status"] == "running"
    _wait_for_status(mcp_server, "wf1", "completed")
    assert engine_calls(mcp_server.client) == [("build_workflow", ("",), {
        "workflow_id": "wf1", "logs": (), "fix_errors": True, "cascade": True,
        "fastapi_port": 8000, "smoke_test_main": False, "serve": False, "await_repair_input": True,
    })]


def test_conversion_failure_keeps_pending_question(mcp_server, monkeypatch):
    mcp_server.client.results["build_nodes"] = object()
    # The pending check is empty before the build and populated once the call completes.
    pending = [{"request_id": "r1", "workflow_id": "wf1", "awaiting": {"stepId": "Reply__amend_node"}}]
    def build(*_args, **_kwargs):
        mcp_server.client.results["pending_requests"] = pending
        return object()
    mcp_server.client.build_nodes = build
    def fail(_result):
        raise ValueError("conversion failed")
    monkeypatch.setattr(mcp_server.module._sdk(), "result_to_dict", fail)
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    question = _wait_for_status(mcp_server, "wf1", "input_required")
    assert question["request_id"] == "r1" and "conversion failed" in question["error"]
    assert call(mcp_server, "abort_background_build", workflow_id="wf1", revision=question["revision"],
                request_id="r1")["status"] == "aborted"


def test_unknown_recovery_retains_exclusive_ownership(mcp_server):
    def build(*_args, **_kwargs):
        mcp_server.client.results["pending_requests"] = RuntimeError("probe failed")
        raise RuntimeError("build failed")
    mcp_server.client.build_nodes = build
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    uncertain = _wait_for_status(mcp_server, "wf1", "recovery_required")
    assert uncertain["action_required"] == "recover_background_build"
    assert not call(mcp_server, "start_build_nodes", workflow_id="wf2")["ok"]
    calls_before = list(mcp_server.client.calls)
    pending = call(mcp_server, "list_pending_builds")
    assert pending["pending"] == [] and pending["count"] == 0
    assert mcp_server.client.calls == calls_before
    mcp_server.client.results["pending_requests"] = [{"workflow_id": "wf1", "request_id": "r1",
                                                      "awaiting": {"stepId": "Reply__amend_node"}}]
    recovered = call(mcp_server, "recover_background_build", workflow_id="wf1", revision=uncertain["revision"])
    assert recovered["status"] == "input_required"
    assert call(mcp_server, "abort_background_build", workflow_id="wf1", revision=recovered["revision"],
                request_id="r1")["status"] == "aborted"


def test_native_cleanup_failure_retains_build_ownership(mcp_server):
    mcp_server.client.build_nodes = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("cleanup failed"))
    mcp_server.client.build_recovery_state = lambda _wid: {"status": "cleanup_required"}
    call(mcp_server, "start_build_nodes", workflow_id="wf1")
    uncertain = _wait_for_status(mcp_server, "wf1", "recovery_required")
    checked = call(mcp_server, "recover_background_build", workflow_id="wf1", revision=uncertain["revision"])
    assert checked["status"] == "recovery_required" and "cleanup" in checked["error"]
    assert not call(mcp_server, "start_build_nodes", workflow_id="wf2")["ok"]
    mcp_server.client.build_recovery_state = lambda _wid: {"status": "clear"}
    assert call(mcp_server, "recover_background_build", workflow_id="wf1", revision=checked["revision"])["status"] == "failed"


def test_background_build_caches_started_service(mcp_server):
    mcp_server.client.results["build_workflow"] = {
        "ok": True, "workflow_id": "wf1", "status": "completed",
        "service": {"base_url": "http://127.0.0.1:8123"},
    }
    call(mcp_server, "start_build_workflow", workflow_id="wf1", prompt="change it", serve=True)
    _wait_for_status(mcp_server, "wf1", "completed")
    assert mcp_server.module._SERVICES["wf1"] == "http://127.0.0.1:8123"


def test_real_mcp_protocol_polls_while_build_runs(mcp_server, monkeypatch):
    import anyio
    started, release = threading.Event(), threading.Event()
    def build(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return {"ok": True, "workflow_id": "wf1", "status": "input_required", "request_id": "r1",
                "awaiting": {"stepId": "Reply__amend_node", "test_log": "pytest failed"}}
    mcp_server.client.build_nodes = build
    monkeypatch.setattr(mcp_server.module, "MCPServerClass", FastMCP)
    server = mcp_server.module.create_server()

    async def exercise():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            response = await session.call_tool("start_build_nodes", {"workflow_id": "wf1"})
            assert json.loads(response.content[0].text)["status"] == "running"
            assert await anyio.to_thread.run_sync(started.wait, 2)
            response = await session.call_tool("get_build_status", {"workflow_id": "wf1"})
            assert json.loads(response.content[0].text)["status"] == "running"
            release.set()
            with anyio.fail_after(5):
                while True:
                    response = await session.call_tool("get_build_status", {"workflow_id": "wf1"})
                    result = json.loads(response.content[0].text)
                    if result["status"] == "input_required":
                        break
                    await anyio.sleep(0.005)
            assert result["awaiting"]["test_log"] == "pytest failed"
            response = await session.call_tool("abort_background_build", {
                "workflow_id": "wf1", "revision": result["revision"], "request_id": "r1"})
            assert json.loads(response.content[0].text)["status"] == "aborted"
    try:
        anyio.run(exercise)
    finally:
        release.set()
