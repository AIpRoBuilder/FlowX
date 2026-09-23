from __future__ import annotations

from pathlib import Path

from flowx_a2a.server import _TaskStore, _dispatch, agent_card
from flowx_sdk import WorkflowArtifacts


class FakeFlowXClient:
    def create_workflow(self, workflow_name: str, requirement: str) -> WorkflowArtifacts:
        assert workflow_name == "ticket_triage"
        assert requirement == "Build a ticket triage workflow"
        return WorkflowArtifacts(
            workflow_name=workflow_name,
            root_dir=Path("/tmp/ticket_triage"),
            requirement_md_path=Path("/tmp/ticket_triage/requirement.md"),
            workflow_json_path=Path("/tmp/ticket_triage/workflow.json"),
            main_entrypoint_path=Path("/tmp/ticket_triage/main.py"),
            backend_port=8123,
        )


def test_agent_card_advertises_a2a_protocol() -> None:
    card = agent_card("http://localhost:8001/")

    assert card["protocolVersion"] == "0.3"
    assert card["url"] == "http://localhost:8001"
    assert card["skills"][0]["id"] == "compile-workflow"


def test_message_send_creates_completed_task_and_task_lookup() -> None:
    tasks = _TaskStore()
    result = _dispatch(
        {
            "method": "message/send",
            "params": {
                "contextId": "context-1",
                "message": {
                    "parts": [{"kind": "text", "text": "Build a ticket triage workflow"}],
                    "metadata": {"workflowName": "ticket_triage"},
                },
            },
        },
        FakeFlowXClient(),  # type: ignore[arg-type]
        tasks,
    )

    assert result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"][0]["data"]["workflowName"] == "ticket_triage"
    fetched = _dispatch({"method": "tasks/get", "params": {"id": result["id"]}}, FakeFlowXClient(), tasks)  # type: ignore[arg-type]
    assert fetched == result


def test_tasks_cancel_marks_working_task_canceled() -> None:
    tasks = _TaskStore()
    task = tasks.create("context-1")

    result = _dispatch({"method": "tasks/cancel", "params": {"id": task.id}}, FakeFlowXClient(), tasks)  # type: ignore[arg-type]

    assert result["status"]["state"] == "canceled"