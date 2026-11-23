# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-10-16: build or modify workflows through FlowXClient.build_workflow.
# Modified 2026-11-01: A2A has no answer channel, so its builds stay non-interactive.
# Modified 2026-11-22: a build is an A2A task that is input-required while contract tasks are open; the calling
#   agent answers with data parts, so the answer channel exists and no LLM runs inside FlowX.
"""A2A JSON-RPC adapter: a calling agent writes the artifacts FlowX asks for."""

from __future__ import annotations

import argparse
import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from flowx_sdk import FlowXClient

_JSONRPC_VERSION = "2.0"
_A2A_PROTOCOL_VERSION = "0.3"
_TERMINAL = {"completed", "failed", "canceled"}


@dataclass
class _Task:
    id: str
    context_id: str
    status: str
    workflow_id: str = ""
    message: Optional[dict[str, Any]] = None
    artifacts: Optional[list[dict[str, Any]]] = None

    def payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contextId"] = payload.pop("context_id")
        payload["metadata"] = {"workflowId": payload.pop("workflow_id")}
        payload["status"] = {"state": payload["status"], "message": payload.pop("message")}
        if payload["artifacts"] is None:
            payload.pop("artifacts")
        return payload


class _TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.RLock()

    def create(self, context_id: str, workflow_id: str = "") -> _Task:
        task = _Task(id=uuid.uuid4().hex, context_id=context_id, status="working", workflow_id=workflow_id)
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Optional[_Task]:
        with self._lock:
            return self._tasks.get(task_id)


def agent_card(base_url: str) -> dict[str, Any]:
    """Return the discovery card defined by the A2A protocol."""
    return {
        "protocolVersion": _A2A_PROTOCOL_VERSION,
        "name": "FlowX",
        "description": (
            "Builds executable AG-UI workflows with you: it sends contract tasks (requirement, graph, one per "
            "node), you write the artifacts, it audits, tests, versions and publishes them."
        ),
        "url": base_url.rstrip("/"),
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "compile-workflow",
                "name": "Compile workflow",
                "description": (
                    "Send a text intent to start a build (metadata.workflowId modifies an existing workflow, "
                    "metadata.target picks 'workflow' or a node). While the task is input-required, answer with a "
                    "data part {task_id, artifacts} (or {submissions: [...]}) for the tasks in its status message."
                ),
                "tags": ["workflow", "agent", "contracts"],
                "examples": ["Build a workflow that summarizes a support ticket."],
            }
        ],
    }


def create_app(client: FlowXClient | None = None) -> Any:
    """Create an A2A-compatible ASGI application.

    It implements agent-card discovery and the non-streaming `message/send`,
    `tasks/get`, and `tasks/cancel` JSON-RPC methods.
    """
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - dependency metadata covers this
        raise ImportError("A2A support requires FastAPI. Install FlowX with its runtime dependencies.") from exc

    flowx = client or FlowXClient()
    tasks = _TaskStore()
    app = FastAPI(title="FlowX A2A", version="0.2.0")

    @app.get("/.well-known/agent-card.json")
    async def get_agent_card(request: Request) -> dict[str, Any]:
        return agent_card(str(request.base_url))

    @app.post("/")
    @app.post("/a2a")
    async def handle_rpc(request: Request) -> JSONResponse:
        request_id: Any = None
        try:
            payload = await request.json()
            request_id = payload.get("id")
            if payload.get("jsonrpc") != _JSONRPC_VERSION:
                raise _RpcError(-32600, "jsonrpc must be '2.0'")
            result = _dispatch(payload, flowx, tasks)
            return JSONResponse({"jsonrpc": _JSONRPC_VERSION, "id": request_id, "result": result})
        except _RpcError as exc:
            return JSONResponse(
                {
                    "jsonrpc": _JSONRPC_VERSION,
                    "id": request_id,
                    "error": {"code": exc.code, "message": exc.message},
                },
                status_code=400,
            )
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            return JSONResponse(
                {
                    "jsonrpc": _JSONRPC_VERSION,
                    "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                },
                status_code=500,
            )

    return app


@dataclass(frozen=True)
class _RpcError(Exception):
    code: int
    message: str


def _text_message(text: str, *data: dict[str, Any]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"kind": "text", "text": text}]
    parts += [{"kind": "data", "data": item} for item in data]
    return {"role": "agent", "parts": parts}


def _refresh(task: _Task, client: FlowXClient) -> None:
    """Mirror the workflow's build state onto its A2A task."""
    state = client.get_build_state(task.workflow_id)
    status = state["status"]
    if status == "running":
        open_tasks = client.get_tasks(task.workflow_id, compact=True)
        if open_tasks:
            task.status = "input-required"
            task.message = _text_message(
                f"{len(open_tasks)} contract task(s) are open. Reply with a data part "
                "{task_id, artifacts} for each (see submit.slots in the task).",
                {"kind": "flowx.tasks", "tasks": open_tasks, "state": state},
            )
        else:
            task.status = "working"
            task.message = _text_message("The build is running.", {"kind": "flowx.state", "state": state})
    elif status == "completed":
        task.status = "completed"
        task.message = _text_message("Workflow built.", {"kind": "flowx.state", "state": state})
        task.artifacts = [{
            "artifactId": "workflow-artifacts",
            "name": task.workflow_id,
            "parts": [{"kind": "data", "data": json.loads(json.dumps(client.artifacts(task.workflow_id), default=str))}],
        }]
    elif status == "aborted":
        task.status = "canceled"
        task.message = _text_message(state.get("error") or "The build was aborted.", {"kind": "flowx.state", "state": state})
    else:
        task.status = "failed"
        report = state.get("error") or "; ".join(f"{key}: {text}" for key, text in state.get("errors", {}).items())
        task.message = _text_message(report or f"The build is {status}.", {"kind": "flowx.state", "state": state})


def _submissions(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``{task_id, artifacts}`` answers carried by a message's data parts."""
    answers: list[dict[str, Any]] = []
    for part in message.get("parts") or []:
        if not isinstance(part, Mapping) or part.get("kind") != "data" or not isinstance(part.get("data"), Mapping):
            continue
        data = part["data"]
        entries = data.get("submissions") if isinstance(data.get("submissions"), list) else [data]
        for entry in entries:
            if not isinstance(entry, Mapping) or "task_id" not in entry:
                raise _RpcError(-32602, "each data part needs a task_id and artifacts")
            answers.append({"task_id": str(entry["task_id"]), "artifacts": dict(entry.get("artifacts") or {})})
    return answers


def _dispatch(payload: Mapping[str, Any], client: FlowXClient, tasks: _TaskStore) -> dict[str, Any]:
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, Mapping):
        raise _RpcError(-32602, "params must be an object")
    if method in ("tasks/get", "tasks/cancel"):
        task = tasks.get(str(params.get("id", "")))
        if task is None:
            raise _RpcError(-32001, "task not found")
        if task.status not in _TERMINAL:
            if method == "tasks/cancel":
                client.abort_build(task.workflow_id, "canceled by the A2A client")
            _refresh(task, client)
        return task.payload()
    if method != "message/send":
        raise _RpcError(-32601, f"unsupported A2A method: {method}")

    message = params.get("message")
    if not isinstance(message, Mapping):
        raise _RpcError(-32602, "message/send requires a message object")
    task_id = message.get("taskId")
    if task_id:
        return _answer(tasks, client, str(task_id), message)
    return _begin(params, message, client, tasks)


def _begin(params: Mapping[str, Any], message: Mapping[str, Any], client: FlowXClient, tasks: _TaskStore) -> dict[str, Any]:
    intent = _message_text(message)
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise _RpcError(-32602, "message metadata must be an object")
    context_id = str(params.get("contextId") or message.get("contextId") or uuid.uuid4().hex)
    task = tasks.create(context_id)
    try:
        task.workflow_id = str(metadata.get("workflowId") or "") or client.create_workflow(
            str(metadata["name"]) if metadata.get("name") else None, intent,
        )
        options: dict[str, Any] = {}
        if metadata.get("target"):
            options["target"] = str(metadata["target"])
        if isinstance(metadata.get("stages"), list):
            options["stages"] = [str(stage) for stage in metadata["stages"]]
        client.begin_build(task.workflow_id, intent, **options)
        _refresh(task, client)
    except Exception as exc:
        task.status = "failed"
        task.message = _text_message(str(exc))
    return task.payload()


def _answer(tasks: _TaskStore, client: FlowXClient, task_id: str, message: Mapping[str, Any]) -> dict[str, Any]:
    task = tasks.get(task_id)
    if task is None:
        raise _RpcError(-32001, "task not found")
    if task.status in _TERMINAL:
        raise _RpcError(-32002, f"task is {task.status} and cannot accept messages")
    answers = _submissions(message)
    if not answers:
        raise _RpcError(-32602, "message must carry a data part with task_id and artifacts")
    results = [client.submit_task(task.workflow_id, item["task_id"], item["artifacts"]) for item in answers]
    _refresh(task, client)
    summary = [
        {key: result.get(key) for key in ("task_id", "accepted", "findings", "error") if key in result}
        for result in results
    ]
    if task.message is not None:
        task.message["parts"].append({"kind": "data", "data": {"kind": "flowx.results", "results": summary}})
    return task.payload()


def _message_text(message: Mapping[str, Any]) -> str:
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise _RpcError(-32602, "message parts must be a list")
    text = "\n".join(
        str(part.get("text", "")).strip()
        for part in parts
        if isinstance(part, Mapping) and part.get("kind") == "text"
    ).strip()
    if not text:
        raise _RpcError(-32602, "message must contain a non-empty text part")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FlowX A2A server")
    parser.add_argument("--host", default=os.environ.get("FLOWX_A2A_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLOWX_A2A_PORT", "8001")))
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)
