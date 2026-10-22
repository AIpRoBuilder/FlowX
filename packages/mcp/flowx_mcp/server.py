# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-10-13: request lifecycle, confirmed resets and active-node selection.
# Modified 2026-10-14: dispose discarded builders and roll back failed new attachments.
# Modified 2026-10-15: document deferred batch orchestration and partial-update risks.
# Modified 2026-10-16: rewrite the tool surface on the AgentBuilder engine (build_*, run_service).
# Modified 2026-10-19: add evolve_workflow and list_nodes.
# Modified 2026-10-22: proxy the generated service's run-step and reset-session endpoints.
"""FastMCP server exposing the FlowX workflow engine."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import io
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterator, Optional

from .bootstrap import bootstrap_import_paths, load_env_from_runtime_context

load_env_from_runtime_context(__file__)
bootstrap_import_paths(__file__)

logger = logging.getLogger("flowx.server")

_MCP_SERVER_AVAILABLE = False
_MCP_SERVER_IMPORT_ERROR = ""
MCPServerClass: Any = None
for module_name, attr_name in (
    ("mcp.server.fastmcp", "FastMCP"),
    ("mcp.server.mcpserver", "MCPServer"),
):
    try:
        MCPServerClass = getattr(importlib.import_module(module_name), attr_name)
        _MCP_SERVER_AVAILABLE = True
        break
    except Exception as exc:
        _MCP_SERVER_IMPORT_ERROR = str(exc)
if not _MCP_SERVER_AVAILABLE:
    MCPServerClass = None


def _json_default(value: Any) -> Any:
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def _error(tool: str, message: str, **extra: Any) -> str:
    return _json({"ok": False, "tool": tool, "error": message, **extra})


@contextlib.contextmanager
def _capture_builder_output(label: str) -> Iterator[None]:
    """Capture noisy builder stdout/stderr so MCP stdio stays clean."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield
    output = buffer.getvalue().strip()
    if output:
        logger.info("%s output:\n%s", label, output)


def _tool_call(tool: str, func: Callable[[], Any]) -> str:
    try:
        with _capture_builder_output(tool):
            payload = func()
        return _json(payload)
    except Exception as exc:
        logger.exception("%s failed", tool)
        return _error(tool, str(exc), traceback=traceback.format_exc().splitlines()[-20:])


def _http(url: str, payload: dict[str, Any], timeout: float) -> str:
    """POST JSON and return the body (SSE text for /api/run-step)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # keep the 404/422 detail visible to the client
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}") from exc


def _wait_until_ready(base_url: str, timeout: float = 15.0) -> None:
    """``rerun_server`` only starts the process; probe GET / before the first step."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=1.0):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"workflow service at {base_url} did not become ready")
            time.sleep(0.2)


def _sse_payloads(text: str) -> list[dict[str, Any]]:
    """``to_sse_payload`` writes one ``data: {json}`` line per AG-UI event."""
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                value = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(value, dict):
                payloads.append(value)
    return payloads


# One client (hence one AgentBuilder) per server process: the core publishes its workflows globally.
_client: Any = None
_client_lock = threading.Lock()

# Services started here, and the input sessions this process keeps per workflow.
_SERVICES: dict[str, str] = {}
_SESSIONS: dict[str, str] = {}
_HTTP_LOCK = threading.Lock()


def _session_for(workflow_id: str) -> str:
    with _HTTP_LOCK:
        return _SESSIONS.setdefault(workflow_id, uuid.uuid4().hex)


def _service_url(workflow_id: str) -> str:
    with _HTTP_LOCK:
        base_url = _SERVICES.get(workflow_id)
    if base_url is None:
        raise RuntimeError(f"workflow {workflow_id} is not running; call run_service first")
    _wait_until_ready(base_url)
    return base_url


def _sdk() -> Any:
    return importlib.import_module("flowx_sdk")


def _get_client() -> Any:
    global _client
    with _client_lock:
        if _client is None:
            _client = _sdk().FlowXClient()
        return _client


def _close_client() -> None:
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


def _build(tool: str, method: str, *args: Any, **kwargs: Any) -> str:
    """Run one engine build method and return its result as JSON."""

    def _impl() -> dict[str, Any]:
        result = getattr(_get_client(), method)(*args, **kwargs)
        return _sdk().result_to_dict(result)

    return _tool_call(tool, _impl)


def create_server() -> Any:
    """Create and return the FlowX MCP server with all tools registered."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server could not load a compatible SDK entry point. "
            "Expected either 'mcp.server.fastmcp.FastMCP' or 'mcp.server.mcpserver.MCPServer'. "
            f"Install or repair the SDK with: {sys.executable} -m pip install -U 'mcp'. "
            f"Last import error: {_MCP_SERVER_IMPORT_ERROR}"
        )

    flowx_mcp = MCPServerClass(
        "flowx",
        instructions=(
            "FlowX workflow builder. Create or modify a workflow's workflow.json and requirements, "
            "its nodes (all of them, or specific ones by step id), its main entrypoint, or the whole "
            "workflow from one request; then run or rerun it as a service. Workflows are identified "
            "by the workflow_id returned when they are created. Stage failures come back as "
            "ok=false with the per-stage errors; pass them to build_workflow(fix_errors=true) to repair. "
            "To change an existing workflow, use evolve_workflow (target the workflow or one node from "
            "list_nodes, or omit target to let the engine decide)."
        ),
    )

    @flowx_mcp.tool()
    def build_workflow_json(
        prompt: str,
        workflow_id: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        """Create (no workflow_id) or modify (with workflow_id) workflow.json and its requirement analysis.

        Nodes and the main entrypoint are not rebuilt; call build_nodes and build_main_entrypoint
        (or build_workflow) afterwards.

        Args:
            prompt: The requirement for a new workflow, or the change to apply to an existing one.
            workflow_id: Id of the workflow to modify; omit to create a new workflow.
            temperature: LLM temperature for graph planning.
        """
        text = str(prompt or "").strip()
        if not text:
            return _error("build_workflow_json", "prompt must be a non-empty string")
        return _build(
            "build_workflow_json", "build_workflow_json", text,
            workflow_id=workflow_id or None, temperature=temperature,
        )

    @flowx_mcp.tool()
    def build_nodes(
        workflow_id: str,
        amendments: Optional[dict[str, str]] = None,
        cascade: bool = False,
    ) -> str:
        """Build a workflow's nodes, or amend specific nodes; unchanged nodes are reused.

        Args:
            workflow_id: Id of an existing workflow that has a workflow.json.
            amendments: Optional {step_id: change description}; only those nodes are modified.
            cascade: Also regenerate the main entrypoint when the nodes changed.
        """
        return _build(
            "build_nodes", "build_nodes", workflow_id,
            dict(amendments) if amendments else None, cascade=cascade,
        )

    @flowx_mcp.tool()
    def build_main_entrypoint(
        workflow_id: str,
        amendment: str = "",
        fastapi_port: int = 8000,
        smoke_test: bool = False,
    ) -> str:
        """Create or regenerate a workflow's main entrypoint from its latest nodes.

        Args:
            workflow_id: Id of a workflow whose nodes are built.
            amendment: Optional change request for the entrypoint.
            fastapi_port: Port the generated service listens on.
            smoke_test: Run the entrypoint once and audit its log before accepting it.
        """
        return _build(
            "build_main_entrypoint", "build_main_entrypoint", workflow_id, amendment,
            fastapi_port=fastapi_port, smoke_test_main=smoke_test,
        )

    @flowx_mcp.tool()
    def build_workflow(
        prompt: str = "",
        workflow_id: Optional[str] = None,
        logs: Optional[list[str]] = None,
        fix_errors: bool = False,
        cascade: bool = True,
        fastapi_port: int = 8000,
        smoke_test: bool = False,
        serve: bool = False,
    ) -> str:
        """Create or modify a whole workflow from a free-text request and/or run logs.

        The request is classified into the stages it affects (workflow.json, nodes, main
        entrypoint); with cascade, later stages follow earlier changes.

        Args:
            prompt: What to create or change.
            workflow_id: Id of the workflow to modify; omit to create a new one.
            logs: Output/test logs of a run; failures in them are traced to a node or the entrypoint and fixed.
            fix_errors: Also repair the errors left unresolved by earlier runs of this workflow.
            cascade: Rebuild later stages when an earlier one changed.
            fastapi_port: Port the generated service listens on.
            smoke_test: Run the entrypoint once and audit its log before accepting it.
            serve: Start the workflow as a service when everything succeeded.
        """
        if not (str(prompt or "").strip() or logs or fix_errors):
            return _error("build_workflow", "provide a prompt, logs, or fix_errors=true")
        return _build(
            "build_workflow", "build_workflow", prompt or "",
            workflow_id=workflow_id or None, logs=tuple(logs or ()), fix_errors=fix_errors,
            cascade=cascade, fastapi_port=fastapi_port, smoke_test_main=smoke_test, serve=serve,
        )

    @flowx_mcp.tool()
    def evolve_workflow(
        workflow_id: str,
        command: str,
        target: Optional[str] = None,
        cascade: bool = True,
        fastapi_port: int = 8000,
        smoke_test: bool = False,
        serve: bool = False,
    ) -> str:
        """Evolve an existing workflow, or one of its nodes, with a change command.

        Args:
            workflow_id: Id of the workflow to evolve.
            command: The change to make.
            target: "workflow" to change the workflow itself, or a node step id / node id
                (see list_nodes) to change only that node; omit to let the engine decide.
            cascade: Rebuild later stages (nodes, main entrypoint) when an earlier one changed.
            fastapi_port: Port the generated service listens on.
            smoke_test: Run the entrypoint once and audit its log before accepting it.
            serve: Start the workflow as a service when everything succeeded.
        """
        if not str(command or "").strip():
            return _error("evolve_workflow", "command must be a non-empty string")
        return _build(
            "evolve_workflow", "evolve", workflow_id, command,
            target=target or None, cascade=cascade, fastapi_port=fastapi_port,
            smoke_test_main=smoke_test, serve=serve,
        )

    @flowx_mcp.tool()
    def run_service(workflow_id: str) -> str:
        """Run a built workflow's main entrypoint as a service; a running one is replaced (rerun).

        Returns the service's ``base_url``; run_step reuses it.

        Args:
            workflow_id: Id of a workflow whose main entrypoint is built.
        """

        def _impl() -> dict[str, Any]:
            service = _get_client().run_service(workflow_id)
            if service.get("base_url"):
                with _HTTP_LOCK:
                    _SERVICES[workflow_id] = service["base_url"]
            return {"ok": True, "service": service}

        return _tool_call("run_service", _impl)

    @flowx_mcp.tool()
    def stop_service(workflow_id: str) -> str:
        """Stop a workflow's running service, if any.

        Args:
            workflow_id: Id of the workflow.
        """

        def _impl() -> dict[str, Any]:
            _get_client().stop_service(workflow_id)
            with _HTTP_LOCK:
                _SERVICES.pop(workflow_id, None)
            return {"ok": True, "workflow_id": workflow_id}

        return _tool_call("stop_service", _impl)

    @flowx_mcp.tool()
    def run_step(
        workflow_id: str,
        step_id: str,
        input: Any = None,
        session_id: str = "",
        file_path: Optional[str] = None,
        timeout: float = 300.0,
    ) -> str:
        """Send one step's input to a running workflow; report whether it needs more input.

        The first call starts the session's run. When the answer is ``status == "input_required"``,
        ``awaiting`` names the step that waits (stepId, title, prompt): ask the user, then call
        run_step again with that step id and the answer. The same session resumes the same native
        run, so completed steps are not re-executed.

        Args:
            workflow_id: Id of a workflow whose service is running (see run_service).
            step_id: Step id to feed, normally the one named by ``awaiting``.
            input: Text, or one file path/base64 payload for a file node.
            session_id: Conversation id; omit to keep one per workflow in this process.
            file_path: Convenience alternative to passing a path in ``input``.
            timeout: Seconds to wait for the step to settle.
        """

        def _impl() -> dict[str, Any]:
            base_url = _service_url(workflow_id)
            session = session_id or _session_for(workflow_id)
            payload: dict[str, Any] = {"sessionId": session, "stepId": step_id, "input": input}
            if file_path:
                payload["file_path"] = file_path
            events = _sse_payloads(_http(f"{base_url}/api/run-step", payload, timeout))
            awaiting = next(
                (event.get("value") for event in events if event.get("name") == "input_required"), None,
            )
            final = next(
                (event["result"] for event in reversed(events) if isinstance(event.get("result"), dict)), None,
            )
            return {
                "ok": True,
                "workflow_id": workflow_id,
                "session_id": session,
                "step_id": step_id,
                "status": "input_required" if awaiting else "completed",
                "awaiting": awaiting,
                "final": final,
                "events": events,
            }

        return _tool_call("run_step", _impl)

    @flowx_mcp.tool()
    def reset_session(workflow_id: str, session_id: str = "") -> str:
        """Cancel this session's paused run and start a fresh one on the next run_step call.

        Args:
            workflow_id: Id of a workflow whose service is running.
            session_id: Session to reset; omit to reset this process's session for the workflow.
        """

        def _impl() -> dict[str, Any]:
            base_url = _service_url(workflow_id)
            session = session_id or _session_for(workflow_id)
            reset = json.loads(_http(f"{base_url}/api/reset-session", {"sessionId": session}, 30.0))
            # The service rotates its runId but keeps the sessionId, so the cache stays valid.
            return {"ok": True, "workflow_id": workflow_id, "session_id": session, "reset": reset}

        return _tool_call("reset_session", _impl)

    @flowx_mcp.tool()
    def list_workflows() -> str:
        """List every workflow with its node ids, versions and unresolved errors."""
        return _tool_call("list_workflows", lambda: {"ok": True, "workflows": _get_client().workflows()})

    @flowx_mcp.tool()
    def list_nodes(workflow_id: str) -> str:
        """List a workflow's nodes as step_id, node_id and version (node_id is null until built).

        Use a step_id or node_id as the target of evolve_workflow.

        Args:
            workflow_id: Id of the workflow.
        """
        return _tool_call(
            "list_nodes", lambda: {"ok": True, "nodes": _get_client().list_nodes(workflow_id)},
        )

    @flowx_mcp.tool()
    def get_workflow(workflow_id: str) -> str:
        """Get the locations of a workflow's requirement, workflow.json, node versions and main entrypoint.

        Args:
            workflow_id: Id of the workflow.
        """
        return _tool_call(
            "get_workflow", lambda: {"ok": True, "workflow": _get_client().artifacts(workflow_id)},
        )

    return flowx_mcp


def run_stdio_server(verbose: bool = False) -> None:
    """Start the FlowX MCP server on stdio."""
    run_server(verbose=verbose)


def run_server(
    *,
    verbose: bool = False,
) -> None:
    """Start the local FlowX MCP server on stdio."""
    if not _MCP_SERVER_AVAILABLE:
        print(
            "Error: MCP server could not load a compatible MCP SDK entry point.\n"
            "Expected either mcp.server.fastmcp.FastMCP or mcp.server.mcpserver.MCPServer.\n"
            f"Install or repair with: {sys.executable} -m pip install -U 'mcp'\n"
            f"Last import error: {_MCP_SERVER_IMPORT_ERROR}",
            file=sys.stderr,
        )
        sys.exit(1)

    load_env_from_runtime_context(__file__)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
    )
    server = create_server()

    try:
        run = getattr(server, "run", None)
        if callable(run):
            run()
            return

        run_stdio_async = getattr(server, "run_stdio_async", None)
        if not callable(run_stdio_async):
            raise RuntimeError(
                "The installed MCP SDK does not expose a compatible server runner. "
                "Expected server.run(...) or server.run_stdio_async()."
            )

        asyncio.run(run_stdio_async())
    except KeyboardInterrupt:
        logger.info("FlowX MCP server interrupted")
    finally:
        # Stops the services this process started.
        _close_client()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run FlowX as a local stdio MCP server")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging on stderr",
    )
    args = parser.parse_args(argv)
    run_server(verbose=bool(args.verbose))


if __name__ == "__main__":  # pragma: no cover
    main()
