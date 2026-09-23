"""A2A JSON-RPC adapter for FlowX workflow compilation."""

from __future__ import annotations

import argparse
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from flowx_sdk import FlowXClient

_JSONRPC_VERSION = "2.0"
_A2A_PROTOCOL_VERSION = "0.3"


@dataclass
class _Task:
    id: str
    context_id: str
    status: str
    message: Optional[dict[str, Any]] = None
    artifacts: Optional[list[dict[str, Any]]] = None

    def payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contextId"] = payload.pop("context_id")
        payload["status"] = {"state": payload["status"], "message": payload.pop("message")}
        if payload["artifacts"] is None:
            payload.pop("artifacts")
        return payload


class _TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.RLock()

    def create(self, context_id: str) -> _Task:
        task = _Task(id=uuid.uuid4().hex, context_id=context_id, status="working")
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
        "description": "Compiles natural-language requirements into executable AG-UI workflows.",
        "url": base_url.rstrip("/"),
        "version": "0.2.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": [
            {
                "id": "compile-workflow",
                "name": "Compile workflow",
                "description": "Build a FlowX workflow from a natural-language requirement.",
                "tags": ["workflow", "agent", "code-generation"],
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


def _dispatch(payload: Mapping[str, Any], client: FlowXClient, tasks: _TaskStore) -> dict[str, Any]:
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, Mapping):
        raise _RpcError(-32602, "params must be an object")
    if method == "tasks/get":
        task = tasks.get(str(params.get("id", "")))
        if task is None:
            raise _RpcError(-32001, "task not found")
        return task.payload()
    if method == "tasks/cancel":
        task = tasks.get(str(params.get("id", "")))
        if task is None:
            raise _RpcError(-32001, "task not found")
        if task.status == "working":
            task.status = "canceled"
        return task.payload()
    if method != "message/send":
        raise _RpcError(-32601, f"unsupported A2A method: {method}")

    message = params.get("message")
    if not isinstance(message, Mapping):
        raise _RpcError(-32602, "message/send requires a message object")
    requirement = _message_text(message)
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise _RpcError(-32602, "message metadata must be an object")
    workflow_name = str(metadata.get("workflowName") or "a2a_workflow")
    context_id = str(params.get("contextId") or message.get("contextId") or uuid.uuid4().hex)
    task = tasks.create(context_id)
    try:
        artifacts = client.create_workflow(workflow_name, requirement)
        task.status = "completed"
        task.artifacts = [
            {
                "artifactId": "workflow-artifacts",
                "name": artifacts.workflow_name,
                "parts": [{"kind": "data", "data": _artifact_data(artifacts)}],
            }
        ]
        task.message = {"role": "agent", "parts": [{"kind": "text", "text": "Workflow compiled."}]}
    except Exception as exc:
        task.status = "failed"
        task.message = {"role": "agent", "parts": [{"kind": "text", "text": str(exc)}]}
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


def _artifact_data(artifacts: Any) -> dict[str, Any]:
    return {
        "workflowName": artifacts.workflow_name,
        "rootDir": str(artifacts.root_dir),
        "requirementMdPath": str(artifacts.requirement_md_path),
        "workflowJsonPath": str(artifacts.workflow_json_path),
        "mainEntrypointPath": str(artifacts.main_entrypoint_path),
        "backendPort": artifacts.backend_port,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the FlowX A2A server")
    parser.add_argument("--host", default=os.environ.get("FLOWX_A2A_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLOWX_A2A_PORT", "8001")))
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)
