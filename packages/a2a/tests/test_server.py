from __future__ import annotations

from pathlib import Path

from flowx_a2a.server import _TaskStore, _dispatch, agent_card
from flowx_core.agent_workflow_engine import AgentResult, StageOutcome


class FakeFlowXClient:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple[str, str | None]] = []

    def build_workflow(self, prompt: str, *, workflow_id: str | None = None) -> AgentResult:
        self.calls.append((prompt, workflow_id))
        return AgentResult(
            workflow_id="wf1",
            ok=self.ok,
            intent=None,
            stages={"graph": StageOutcome("graph", "completed" if self.ok else "failed", 1)},
            errors={} if self.ok else {"graph": "RuntimeError: audit did not pass"},
            logs={},
            artifacts={"workflow_id": "wf1", "graph": Path("/tmp/wf1/workflow.json")},
        )


def _message(workflow_id: str | None = None) -> dict:
    message: dict = {"parts": [{"kind": "text", "text": "Build a ticket triage workflow"}]}
    if workflow_id:
        message["metadata"] = {"workflowId": workflow_id}
    return {"method": "message/send", "params": {"contextId": "context-1", "message": message}}


def test_agent_card_advertises_a2a_protocol() -> None:
    card = agent_card("http://localhost:8001/")

    assert card["protocolVersion"] == "0.3"
    assert card["url"] == "http://localhost:8001"
    assert card["skills"][0]["id"] == "compile-workflow"


def test_message_send_creates_completed_task_and_task_lookup() -> None:
    tasks, client = _TaskStore(), FakeFlowXClient()
    result = _dispatch(_message(), client, tasks)  # type: ignore[arg-type]

    assert client.calls == [("Build a ticket triage workflow", None)]
    assert result["status"]["state"] == "completed"
    data = result["artifacts"][0]["parts"][0]["data"]
    assert data["workflow_id"] == "wf1"
    assert data["artifacts"]["graph"] == "/tmp/wf1/workflow.json"
    fetched = _dispatch({"method": "tasks/get", "params": {"id": result["id"]}}, client, tasks)  # type: ignore[arg-type]
    assert fetched == result


def test_message_send_modifies_the_workflow_named_in_metadata() -> None:
    client = FakeFlowXClient()

    _dispatch(_message("wf1"), client, _TaskStore())  # type: ignore[arg-type]

    assert client.calls == [("Build a ticket triage workflow", "wf1")]


def test_failed_stage_marks_the_task_failed_with_the_stage_errors() -> None:
    result = _dispatch(_message(), FakeFlowXClient(ok=False), _TaskStore())  # type: ignore[arg-type]

    assert result["status"]["state"] == "failed"
    assert "graph: RuntimeError: audit did not pass" in result["status"]["message"]["parts"][0]["text"]
    assert result["artifacts"][0]["parts"][0]["data"]["ok"] is False


def test_tasks_cancel_marks_working_task_canceled() -> None:
    tasks = _TaskStore()
    task = tasks.create("context-1")

    result = _dispatch({"method": "tasks/cancel", "params": {"id": task.id}}, FakeFlowXClient(), tasks)  # type: ignore[arg-type]

    assert result["status"]["state"] == "canceled"