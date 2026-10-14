# SPDX-License-Identifier: Apache-2.0
"""HTTP/SSE fixtures mirror engine._finish_payload and WorkflowEventFactory."""

import json

import pytest


def test_paused_run_finished_retains_waiting_metadata(mcp_modules, fake_http):
    client, _ = mcp_modules
    requests, responses = fake_http
    waiting = {"stepId": "ActualGate", "title": "Answer", "prompt": "继续?"}
    card = {"stepId": "Prepare", "card": {"text": "ready"}, "isFinal": False}
    responses.append([
        {"type": "RUN_STARTED", "threadId": "graph", "runId": "request"},
        {"type": "STEP_STARTED", "stepName": "Prepare"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "ready"},
        {"type": "CUSTOM", "name": "step_card", "value": card},
        {"type": "CUSTOM", "name": "input_required", "value": waiting},
        {"type": "RUN_FINISHED", "result": {
            "ok": True, "stepId": "Prepare", "isFinal": False,
            "completedSteps": ["Prepare"], "awaitingInput": "ActualGate",
        }},
    ])

    result = client.run_step_sse(
        "http://stub", session_id="old", step_id="Entry", input={"text": "hello"}
    )

    assert result["ok"] is True
    assert result["status"] == "awaiting_input"
    assert result["awaitingInput"] == "ActualGate"
    assert result["input_required"] == waiting
    assert result["isFinal"] is False
    assert result["stepId"] == "Prepare"
    assert result["completedSteps"] == ["Prepare"]
    assert result["text"] == "ready"
    assert result["cards"] == [card]
    assert result["steps_started"] == ["Prepare"]
    assert result["error"] is None
    assert json.loads(requests[0][0].data) == {
        "sessionId": "old", "stepId": "Entry", "input": {"text": "hello"},
    }


@pytest.mark.parametrize("final", [{"answer": 42}, [], "done", 0, False, None])
def test_terminal_derived_data_is_preserved(mcp_modules, fake_http, final):
    client, _ = mcp_modules
    _, responses = fake_http
    responses.append([{"type": "RUN_FINISHED", "result": {
        "ok": True, "stepId": "Terminal", "isFinal": True,
        "completedSteps": ["Entry", "Terminal"], "final": final,
    }}])
    result = client.run_step_sse("http://stub", session_id="s", step_id="Entry")
    assert result["status"] == "completed"
    assert result["isFinal"] is True
    assert result["final"] == final
    assert result["awaitingInput"] is None
    assert result["input_required"] is None


@pytest.mark.parametrize("custom", [None, {"stepId": "OtherGate", "title": "wrong"}])
def test_finish_waiting_id_wins_even_after_a_terminal_branch(mcp_modules, fake_http, custom):
    client, _ = mcp_modules
    _, responses = fake_http
    events = []
    if custom is not None:
        events.append({"type": "CUSTOM", "name": "input_required", "value": custom})
    events.append({"type": "RUN_FINISHED", "result": {
        "ok": True, "stepId": "TerminalBranch", "isFinal": True,
        "completedSteps": ["TerminalBranch"], "final": {"partial": True},
        "awaitingInput": "ActualGate",
    }})
    responses.append(events)
    result = client.run_step_sse("http://stub", session_id="s", step_id="Entry")
    assert result["status"] == "awaiting_input"
    assert result["awaitingInput"] == "ActualGate"
    assert result["input_required"] == {"stepId": "ActualGate"}
    assert result["isFinal"] is False
    assert result["final"] == {"partial": True}


def test_custom_input_required_without_finish_id(mcp_modules, fake_http):
    client, _ = mcp_modules
    _, responses = fake_http
    responses.append([
        {"type": "CUSTOM", "name": "input_required", "value": {"stepId": "Gate"}},
        {"type": "RUN_FINISHED", "result": {"ok": True, "completedSteps": []}},
    ])
    result = client.run_step_sse("http://stub", session_id="s", step_id="Entry")
    assert result["status"] == "awaiting_input"
    assert result["awaitingInput"] == "Gate"


def test_run_error_is_not_completion(mcp_modules, fake_http):
    client, _ = mcp_modules
    _, responses = fake_http
    responses.append([
        {"type": "RUN_ERROR", "message": "bad input"},
        {"type": "RUN_FINISHED", "result": {"ok": False, "stepId": "Gate"}},
    ])
    result = client.run_step_sse("http://stub", session_id="s", step_id="Entry")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["error"] == "bad input"
    assert result["isFinal"] is False


def test_truncated_stream_is_not_success(mcp_modules, fake_http):
    client, _ = mcp_modules
    _, responses = fake_http
    responses.append([{"type": "TEXT_MESSAGE_CONTENT", "delta": "partial"}])
    result = client.run_step_sse("http://stub", session_id="s", step_id="Entry")
    assert result["ok"] is False
    assert result["status"] == "incomplete"
    assert result["text"] == "partial"
    assert result["error"]


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"error": "not reset"}'])
def test_reset_requires_explicit_success(mcp_modules, fake_http, body):
    client, _ = mcp_modules
    _, responses = fake_http
    responses.append(body)
    assert client.reset_session("http://stub", "old")["ok"] is False