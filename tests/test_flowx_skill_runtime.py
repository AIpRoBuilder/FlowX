# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "flowx-workflows" / "scripts" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("flowx_skill_runtime", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class Response:
    def __init__(self, content=b""):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.content


@pytest.fixture
def harness(monkeypatch, tmp_path):
    state = SimpleNamespace(
        workspace=None, workflow=None, closed=False, lifecycle=[],
        service={"base_url": "http://127.0.0.1:8123"},
        start_error=None, close_error=None,
    )

    class Client:
        def __init__(self, *, workspace):
            state.workspace = workspace
            print("SDK constructor output")

        def __enter__(self):
            state.lifecycle.append("enter")
            return self

        def run_service(self, workflow_id):
            state.lifecycle.append("start")
            state.workflow = workflow_id
            print("SDK startup output")
            if state.start_error:
                raise state.start_error
            return state.service

        def __exit__(self, *args):
            state.lifecycle.append("close")
            state.closed = True
            print("SDK close output")
            if state.close_error:
                raise state.close_error

    output = Output()
    errors = io.StringIO()
    monkeypatch.setattr(runtime, "_check_service_setup", lambda client, workflow_id: SimpleNamespace(process=None))
    monkeypatch.setitem(sys.modules, "flowx_sdk", SimpleNamespace(FlowXClient=Client))
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    state.output = output
    state.stderr = errors
    state.argv = ["--workspace", str(tmp_path), "--workflow-id", "workflow-1"]
    state.events = lambda: [json.loads(line) for line in output.getvalue().splitlines()]

    def run(argv):
        monkeypatch.setattr(sys, "stdout", output)
        monkeypatch.setattr(sys, "stderr", errors)
        return runtime.main(argv)

    state.run = run
    return state


def test_step_resume_reset_and_stop_keep_one_client_and_session(harness, monkeypatch):
    requests = []
    responses = iter([
        b'data: {"type":"CUSTOM","name":"input_required","value":{"stepId":"ask","prompt":"Name?"}}\n\n'
        b'data: {"type":"RUN_FINISHED","result":{"ok":true,"awaitingInput":"ask"}}\n\n',
        b'data: {"type":"RUN_FINISHED","result":{"ok":true,"isFinal":true}}\n\n',
        b'{"ok":true,"sessionId":"session-1","threadId":"t1","runId":"r2"}',
    ])

    def urlopen(request, timeout):
        if isinstance(request, str):
            assert request == "http://127.0.0.1:8123/"
            assert harness.lifecycle == ["enter", "start"]
            requests.append(("GET", request, timeout))
            return Response()
        requests.append((request.method, request.full_url, json.loads(request.data), timeout,
                         request.get_header("Content-type"), request.get_header("Accept")))
        return Response(next(responses))

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        '{"action":"step","step_id":"prepare"}\n'
        '{"action":"step","step_id":"ask","input":{"name":"Ada"},"file_path":"/tmp/input"}\n'
        '{"action":"reset"}\n'
        '{"action":"stop"}\n'
        '{"action":"step","step_id":"must-not-run"}\n'
    ))

    code = harness.run(harness.argv + ["--session-id", "session-1", "--timeout", "3.5"])

    events = harness.events()
    assert code == 0
    assert harness.workspace == harness.argv[1] and harness.workflow == "workflow-1"
    assert harness.lifecycle == ["enter", "start", "close"] and harness.closed
    assert [event["event"] for event in events] == ["service_ready", "step", "step", "reset", "service_stopped"]
    assert events[0] == {"event": "service_ready", "base_url": "http://127.0.0.1:8123",
                         "session_id": "session-1", "workflow_id": "workflow-1"}
    assert events[1]["status"] == "input_required"
    assert events[1]["awaiting"] == {"stepId": "ask", "prompt": "Name?"}
    assert events[1]["final"] == {"ok": True, "awaitingInput": "ask"}
    assert len(events[1]["events"]) == 2
    assert events[2]["status"] == "response_received" and events[2]["final"]["isFinal"] is True
    assert events[3]["reset"]["runId"] == "r2"
    assert requests[1:] == [
        ("POST", "http://127.0.0.1:8123/api/run-step",
         {"sessionId": "session-1", "stepId": "prepare", "input": None}, 3.5,
         "application/json", "text/event-stream"),
        ("POST", "http://127.0.0.1:8123/api/run-step",
         {"sessionId": "session-1", "stepId": "ask", "input": {"name": "Ada"}, "file_path": "/tmp/input"},
         3.5, "application/json", "text/event-stream"),
        ("POST", "http://127.0.0.1:8123/api/reset-session", {"sessionId": "session-1"},
         3.5, "application/json", "text/event-stream"),
    ]
    assert harness.output.flushes == len(events)
    assert "SDK constructor output" in harness.stderr.getvalue()
    assert "SDK startup output" in harness.stderr.getvalue() and "SDK close output" in harness.stderr.getvalue()


def test_sse_parsing_preserves_error_events_and_last_result_without_claiming_success(monkeypatch):
    body = (
        ': comment\nnot data\ndata: [1,2]\ndata: invalid json\n'
        'data: {"type":"RUN_ERROR","message":"node failed"}\n\n'
        'data: {"result":{"ok":true}}\ndata: {"result":{"ok":false,"reason":"failed"}}\n'
    )
    monkeypatch.setattr(runtime, "_post", lambda *args: body)

    result = runtime._step({"step_id": "s"}, "http://localhost:8000", "session", 1)

    assert result["status"] == "response_received"
    assert result["awaiting"] is None and result["final"] == {"ok": False, "reason": "failed"}
    assert result["events"][0] == {"type": "RUN_ERROR", "message": "node failed"}
    assert len(result["events"]) == 3


def test_sse_input_required_uses_name_and_value_even_if_falsy(monkeypatch):
    monkeypatch.setattr(runtime, "_post", lambda *args: 'data: {"name":"input_required","value":{}}\n')
    result = runtime._step({"step_id": "s"}, "http://localhost", "session", 1)
    assert result["status"] == "input_required" and result["awaiting"] == {}
    assert "final" not in result
    assert runtime._sse_payloads('data: {"value":NaN}\ndata: null\ndata: {"value":1}\n') == [{"value": 1}]


def test_sse_preserves_last_result_even_when_not_an_object(monkeypatch):
    monkeypatch.setattr(runtime, "_post", lambda *args: 'data: {"result":{"ok":true}}\ndata: {"result":null}\n')
    result = runtime._step({"step_id": "s"}, "http://localhost", "session", 1)
    assert result["final"] is None


def test_invalid_commands_and_http_errors_do_not_end_session_or_echo_secrets(harness, monkeypatch):
    calls = []

    def urlopen(request, timeout):
        if isinstance(request, str):
            return Response()
        calls.append(request.full_url)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 422, "secret reason", {}, io.BytesIO(b"secret body"))
        if len(calls) == 2:
            return Response(b'not JSON with secret')
        return Response(b'{"ok":true}')

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        'invalid secret\n[]\n{"action":"secret"}\n'
        '{"action":"step","input":"secret"}\n'
        '{"action":"step","step_id":"s","file_path":""}\n'
        '{"action":"step","step_id":"s","input":NaN}\n'
        '{"action":"step","step_id":"s","input":"secret"}\n'
        '{"action":"reset"}\n{"action":"reset"}\n{"action":"stop"}\n'
    ))

    assert harness.run(harness.argv) == 0
    events = harness.events()
    assert [event["event"] for event in events] == ["service_ready"] + ["error"] * 8 + ["reset", "service_stopped"]
    assert events[7]["operation"] == "step" and events[7]["http_status"] == 422
    assert events[8]["operation"] == "reset" and events[8]["error"] == "invalid JSON response"
    assert "secret" not in harness.output.getvalue()
    assert len(calls) == 3 and harness.closed


def test_request_exception_is_sanitized_and_the_session_remains_usable(harness, monkeypatch):
    calls = []

    def urlopen(request, timeout):
        if isinstance(request, str):
            return Response()
        calls.append(request.full_url)
        if len(calls) == 1:
            raise RuntimeError("credential or input value")
        return Response(b'data: {"type":"RUN_FINISHED","result":{"ok":false}}\n')

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        '{"action":"step","step_id":"s"}\n{"action":"step","step_id":"s"}\n'
    ))
    assert harness.run(harness.argv) == 0
    assert [event["event"] for event in harness.events()] == ["service_ready", "error", "step", "service_stopped"]
    assert harness.events()[1]["error_type"] == "RuntimeError"
    assert harness.events()[2]["final"] == {"ok": False}
    assert "credential" not in harness.output.getvalue() and harness.closed


def test_eof_closes_client_and_generates_a_uuid(harness, monkeypatch):
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert harness.run(harness.argv) == 0
    ready, stopped = harness.events()
    assert uuid.UUID(ready["session_id"]).version == 4
    assert stopped["session_id"] == ready["session_id"] and harness.closed


def test_ready_event_is_flushed_before_reading_input(harness, monkeypatch):
    class Input:
        def __iter__(self):
            assert harness.events()[0]["event"] == "service_ready"
            assert harness.output.flushes == 1
            yield '{"action":"stop"}\n'

    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    monkeypatch.setattr(sys, "stdin", Input())
    assert harness.run(harness.argv) == 0
    assert harness.closed and harness.events()[-1]["event"] == "service_stopped"


def test_readiness_retries_with_a_bounded_deadline(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    attempts = []

    def urlopen(url, timeout):
        attempts.append((url, timeout))
        if len(attempts) < 3:
            raise OSError("connection refused")
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    runtime._wait_until_ready("http://localhost:80", 0.5)
    assert len(attempts) == 3 and clock.sleeps == [0.2, 0.2]
    assert attempts[0][1] == 0.5 and attempts[-1][1] == pytest.approx(0.1)


def test_check_service_setup_rejects_an_occupied_port(monkeypatch):
    import socket
    from flowx_core.tools import agent_builder_tools

    monkeypatch.setattr(agent_builder_tools, "select_python_command", lambda: sys.executable)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        record = SimpleNamespace(fastapi_host="127.0.0.1", fastapi_port=listener.getsockname()[1])
        client = SimpleNamespace(builder=SimpleNamespace(engine=SimpleNamespace(record=lambda _: record)))
        with pytest.raises(runtime.ServiceConfigurationError, match="unavailable"):
            runtime._check_service_setup(client, "wf1")


def test_check_service_setup_rejects_a_different_interpreter(monkeypatch):
    from flowx_core.tools import agent_builder_tools

    monkeypatch.setattr(agent_builder_tools, "select_python_command", lambda: __file__)
    with pytest.raises(runtime.ServiceConfigurationError, match="interpreter differs"):
        runtime._check_service_setup(None, "wf1")


def test_readiness_rejects_a_child_that_has_exited(monkeypatch):
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(runtime.ServiceConfigurationError, match="exited"):
        runtime._wait_until_ready("http://localhost:8000", 1, SimpleNamespace(poll=lambda: 1))


def test_readiness_timeout_closes_service_and_does_not_read_commands(harness, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(runtime, "time", SimpleNamespace(
        monotonic=lambda: clock.now, sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"action":"stop"}\n'))

    assert harness.run(harness.argv + ["--ready-timeout", "0.3"]) == 1
    assert harness.closed and harness.lifecycle == ["enter", "start", "close"]
    assert harness.events() == [{"event": "error", "operation": "readiness",
                                "error": "service readiness timed out", "error_type": "TimeoutError"}]


def test_readiness_http_error_is_closed_before_retry(monkeypatch):
    body = io.BytesIO(b"sensitive response")
    attempts = []

    def urlopen(url, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(url, 503, "not ready", {}, body)
        return Response()

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(runtime.time, "sleep", lambda seconds: None)
    runtime._wait_until_ready("http://localhost:80", 1)
    assert body.closed and len(attempts) == 2


def test_interrupt_closes_client(harness, monkeypatch):
    def urlopen(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime.urllib.request, "urlopen", urlopen)
    assert harness.run(harness.argv) == 130
    assert harness.closed and harness.events() == [{"event": "error", "operation": "readiness", "error": "interrupted"}]


@pytest.mark.parametrize("service", [
    {}, {"base_url": "http://localhost:abc"}, {"base_url": "http://[invalid"},
    {"base_url": "http://localhost/?key=secret"}, {"base_url": "http://localhost/path"},
    {"base_url": "file:///tmp/service"},
])
def test_invalid_base_url_fails_safely_and_closes_service(harness, service):
    harness.service = service
    assert harness.run(harness.argv) == 1
    assert harness.closed and harness.events()[0]["error_type"] == "ServiceConfigurationError"
    assert "secret" not in harness.output.getvalue()


def test_ipv6_service_address_is_supported():
    assert runtime._base_url({"base_url": "http://[::1]:8000/"}) == "http://[::1]:8000"


@pytest.mark.parametrize("failure", ["start", "base_url", "close"])
def test_startup_and_cleanup_errors_are_reported_without_secrets(harness, monkeypatch, failure):
    if failure == "start":
        harness.start_error = RuntimeError("secret startup failure")
    elif failure == "base_url":
        harness.service = {"base_url": "http://user:secret@localhost:8000"}
    else:
        harness.close_error = RuntimeError("secret cleanup failure")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    assert harness.run(harness.argv) == 1
    assert harness.closed
    assert harness.events()[-1]["event"] == "error"
    assert harness.events()[-1]["operation"] == ("shutdown" if failure == "close" else "startup")
    assert "secret" not in harness.output.getvalue()
    assert not any(event["event"] == "service_stopped" for event in harness.events())


def test_help_is_standalone_and_documents_protocol():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, check=True)
    for text in ("--workspace", "--workflow-id", "--session-id", "--timeout", "--ready-timeout",
                 '"action":"step"', '"action":"reset"', '"action":"stop"', "service_ready", "events"):
        assert text in result.stdout
    assert not result.stderr


def test_standalone_process_exchanges_json_lines_with_mock_sdk(tmp_path):
    # The script imports this isolated fake instead of starting a service or making HTTP calls.
    (tmp_path / "flowx_sdk.py").write_text('''\
import json
import urllib.request
from types import SimpleNamespace

print("SDK import log")

class Response:
    def __init__(self, content=b""):
        self.content = content
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self.content

def urlopen(request, timeout):
    if isinstance(request, str):
        return Response()
    payload = json.loads(request.data)
    if request.full_url.endswith("/api/run-step"):
        assert payload == {"sessionId": "s1", "stepId": "ask", "input": "Ada"}
        return Response(b'data: {"type":"RUN_ERROR","message":"failed"}\\n\\n')
    assert request.full_url.endswith("/api/reset-session")
    assert payload == {"sessionId": "s1"}
    return Response(b'{"ok":true,"runId":"next"}')

class FlowXClient:
    def __init__(self, *, workspace):
        assert workspace == "workspace"
        urllib.request.urlopen = urlopen
        record = SimpleNamespace(fastapi_host="127.0.0.1", fastapi_port=0, process=None)
        self.builder = SimpleNamespace(engine=SimpleNamespace(record=lambda workflow_id: record))
    def __enter__(self):
        return self
    def run_service(self, workflow_id):
        assert workflow_id == "wf1"
        print("SDK startup log")
        return {"base_url": "http://127.0.0.1:8123"}
    def __exit__(self, *args):
        print("SDK cleanup log")
''', encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", "workspace", "--workflow-id", "wf1", "--session-id", "s1"],
        input='{"action":"step","step_id":"ask","input":"Ada"}\n{"action":"reset"}\n{"action":"stop"}\n',
        capture_output=True, text=True, env=env, timeout=10, check=True,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["event"] for event in events] == ["service_ready", "step", "reset", "service_stopped"]
    assert events[1]["events"] == [{"type": "RUN_ERROR", "message": "failed"}]
    assert events[1]["status"] == "response_received" and events[2]["reset"]["runId"] == "next"
    assert "SDK import log" in result.stderr and "SDK startup log" in result.stderr and "SDK cleanup log" in result.stderr


@pytest.mark.parametrize("flag,value", [
    ("--timeout", "0"), ("--timeout", "nan"), ("--ready-timeout", "inf"),
    ("--workflow-id", " "), ("--session-id", ""),
])
def test_cli_rejects_invalid_values_before_starting_service(harness, flag, value):
    with pytest.raises(SystemExit) as exc:
        harness.run(harness.argv + [flag, value])
    assert exc.value.code == 2 and harness.lifecycle == []