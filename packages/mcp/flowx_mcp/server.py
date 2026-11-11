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
# Modified 2026-11-01: interactive repair gating is the default for every build tool.
# Modified 2026-11-02: paused builds expose action_required and list_pending_builds for recovery.
# Modified 2026-11-02: reuse shared build result handling for submitted build input.
# Modified 2026-11-02: capture pending-build probe errors and describe the actual pause metadata.
# Modified 2026-11-03: add workflow-indexed background builds and guarded repair recovery.
# Modified 2026-11-03: expose background-only MCP build tools and guard orphaned pauses.
# Modified 2026-11-11: accept an optional workflow folder name at allocation.
"""FastMCP server exposing the FlowX workflow engine."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import importlib
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

_OUTPUT_LOCK = threading.RLock()
_OUTPUT_DEPTH = 0
_OUTPUT_ORIGINAL: Any = None

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
    """Route builder output to stderr while the stdio transport owns its original stream.

    The transport binds stdout before tools run. Nested and concurrent calls must share
    one redirect; a logger initialized during a build may retain the stderr stream.
    """
    del label
    global _OUTPUT_DEPTH, _OUTPUT_ORIGINAL
    with _OUTPUT_LOCK:
        if _OUTPUT_DEPTH == 0:
            _OUTPUT_ORIGINAL = sys.stdout
            sys.stdout = sys.stderr
        _OUTPUT_DEPTH += 1
    try:
        yield
    finally:
        with _OUTPUT_LOCK:
            _OUTPUT_DEPTH -= 1
            if _OUTPUT_DEPTH == 0:
                sys.stdout = _OUTPUT_ORIGINAL
                _OUTPUT_ORIGINAL = None


def _tool_call(tool: str, func: Callable[[], Any]) -> str:
    try:
        with _capture_builder_output(tool):
            payload = func()
        return _json(payload)
    except Exception as exc:
        logger.exception("%s failed", tool)
        return _error(tool, str(exc), traceback=traceback.format_exc().splitlines()[-20:])


def _status_call(tool: str, func: Callable[[], Any]) -> str:
    """Registry-only tools must not touch the process-wide output stream."""
    try:
        return _json(func())
    except Exception as exc:
        return _error(tool, str(exc))


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

# The engine serializes all builds, including different workflows. This registry
# has its own lock so polling never waits for the engine's long-running lock.
_BUILD_LOCK = threading.RLock()
_BUILD_JOBS: dict[str, dict[str, Any]] = {}
_BUILD_ACTIVE: str | None = None


def _reserve_build(owner: str) -> None:
    global _BUILD_ACTIVE
    with _BUILD_LOCK:
        if _BUILD_ACTIVE is not None:
            raise RuntimeError(f"another build is active ({_BUILD_ACTIVE}); query or finish it first")
        _BUILD_ACTIVE = owner


def _release_build(owner: str) -> None:
    global _BUILD_ACTIVE
    with _BUILD_LOCK:
        if _BUILD_ACTIVE == owner:
            _BUILD_ACTIVE = None


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """Copy the last observation without exposing mutable registry state."""
    return copy.deepcopy(job)


def _job_action(job: dict[str, Any]) -> None:
    job["action_required"] = (
        "submit_background_build_input" if job["status"] == "input_required"
        else "recover_background_build" if job["status"] == "recovery_required"
        else "get_build_status" if job["status"] == "running" else None
    )


def _check_build_recovery(workflow_id: str) -> dict[str, Any]:
    return dict(_get_client().build_recovery_state(workflow_id))


def _run_background(workflow_id: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Run to the next native pause/completion, then publish an immutable snapshot."""
    global _BUILD_ACTIVE
    payload: dict[str, Any] | None = None
    error = ""
    recovery: dict[str, Any] | None = None
    recovery_unknown = False
    cleanup_required = False
    invoked = False
    try:
        with _capture_builder_output(method):
            if method != "answer":
                busy = _pending_payload(method)
                if busy is not None:
                    raise RuntimeError(busy["error"])
            invoked = True
            result = getattr(_get_client(), method)(*args, **kwargs)
            payload = dict(_sdk().result_to_dict(result))
            # Validate serializability before publishing the result to pollers.
            _json(payload)
            service = payload.get("service")
            if isinstance(service, dict) and service.get("base_url"):
                with _HTTP_LOCK:
                    _SERVICES[workflow_id] = service["base_url"]
    except BaseException as exc:
        logger.exception("background %s failed", method)
        error = f"{type(exc).__name__}: {exc}"
        if invoked:
            try:
                state = _check_build_recovery(workflow_id)
                recovery = state if state.get("status") == "input_required" else None
                cleanup_required = state.get("status") == "cleanup_required"
            except Exception:
                recovery_unknown = True
                logger.exception("could not inspect the failed build")
    with _BUILD_LOCK:
        job = _BUILD_JOBS[workflow_id]
        job["revision"] += 1
        if recovery is not None:
            job.update(status="input_required", request_id=recovery["request_id"],
                       awaiting=recovery["awaiting"], error=error, result=None)
        elif recovery_unknown or cleanup_required:
            job.update(status="recovery_required", request_id=None, awaiting=None, error=error, result=None)
        elif error:
            job.update(status="failed", request_id=None, awaiting=None, error=error, result=None)
        elif payload is not None and payload.get("status") == "input_required":
            job.update(status="input_required", request_id=payload.get("request_id"),
                       awaiting=payload.get("awaiting"), error=None, result=payload)
        else:
            job.update(status="completed" if payload and payload.get("ok") else "failed",
                       request_id=None, awaiting=None, error=None, result=payload)
        _job_action(job)
        if job["status"] not in ("input_required", "recovery_required") and _BUILD_ACTIVE == workflow_id:
            _BUILD_ACTIVE = None


def _start_background(_workflow_id: str, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Reserve the single writer before launching; return the running observation."""
    global _BUILD_ACTIVE
    workflow_id = _workflow_id
    if not workflow_id.strip():
        raise ValueError("workflow_id must be a non-empty string")
    with _BUILD_LOCK:
        _reserve_build(workflow_id)
        previous = _BUILD_JOBS.get(workflow_id)
        revision = (previous["revision"] if previous else 0) + 1
        job = {"ok": True, "workflow_id": workflow_id, "revision": revision,
               "status": "running", "request_id": None, "awaiting": None,
               "result": None, "error": None}
        _job_action(job)
        _BUILD_JOBS[workflow_id] = job
        initial = _job_snapshot(job)
        try:
            threading.Thread(target=_run_background, args=(workflow_id, method, args, kwargs),
                             name=f"flowx-build-{workflow_id}", daemon=True).start()
        except Exception:
            if previous is None:
                _BUILD_JOBS.pop(workflow_id, None)
            else:
                _BUILD_JOBS[workflow_id] = previous
            _BUILD_ACTIVE = None
            raise
        return initial


def _resume_background(workflow_id: str, revision: int, request_id: str, step_id: str, value: str) -> dict[str, Any]:
    with _BUILD_LOCK:
        job = _BUILD_JOBS.get(workflow_id)
        if job is None:
            raise LookupError(f"no background build for workflow {workflow_id}")
        if (job["status"] != "input_required" or job["revision"] != revision
                or job["request_id"] != request_id or _BUILD_ACTIVE != workflow_id):
            raise ValueError("stale or already answered build; call get_build_status again")
        awaiting = job.get("awaiting") or {}
        if not step_id or step_id != awaiting.get("stepId"):
            raise ValueError("step_id does not match the current question")
        old = _job_snapshot(job)
        job.update(status="running", revision=revision + 1, request_id=None,
                   awaiting=None, result=None, error=None)
        _job_action(job)
        initial = _job_snapshot(job)
        try:
            threading.Thread(target=_run_background,
                             args=(workflow_id, "answer", (request_id, value, step_id), {}),
                             name=f"flowx-answer-{workflow_id}", daemon=True).start()
        except Exception:
            _BUILD_JOBS[workflow_id] = old
            raise
        return initial


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


def _pending_payload(tool: str) -> dict[str, Any] | None:
    """Refuse a new build while one waits at a node gate (it owns its candidate revision)."""
    pending = _get_client().pending_requests()
    if not pending:
        return None
    first = pending[0]
    return {
        "ok": False, "tool": tool, "status": "blocked",
        "request_id": first["request_id"],
        "workflow_id": first["workflow_id"],
        "error": (
            f"unregistered paused build request_id={first['request_id']} "
            f"workflow_id={first['workflow_id']} owns a candidate; resolve it outside "
            "this MCP server before starting another build"
        ),
        "awaiting": first["awaiting"],
    }


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
            "workflow from one request; then run or rerun it as a service. Use create_workflow "
            "to allocate a workflow_id, then start_new_workflow for the whole workflow or "
            "start_build_workflow_json for its graph and requirements. For existing workflows use "
            "start_build_workflow_json, start_build_nodes, start_build_main_entrypoint, "
            "start_build_workflow or start_evolve_workflow. To target a node with "
            "start_evolve_workflow, obtain its step_id or node_id from list_nodes; omit target "
            "to let the engine decide. Stage failures include per-stage errors; use "
            "start_build_workflow with fix_errors=true to repair them. Start tools return "
            "immediately: poll get_build_status with workflow_id until input_required, completed, "
            "failed or recovery_required. The top-level ok indicates a successful status query, "
            "not a successful build: inspect status and result.ok. Interactive builds pause on a node test failure. Show "
            "awaiting.prompt and awaiting.test_log to the user, then call "
            "submit_background_build_input with the exact revision, request_id and awaiting.stepId, "
            "or abort_background_build with the exact revision and request_id. Use "
            "list_pending_builds to rediscover an active or paused background build. Do not retry "
            "a build while it is active or paused; if status=recovery_required call "
            "recover_background_build. Background status and paused runs exist only in this "
            "server process."
        ),
    )

    @flowx_mcp.tool()
    def create_workflow(name: str | None = None) -> str:
        """Allocate an empty workflow; name, if supplied, is its folder and workflow_id."""
        def _impl() -> dict[str, Any]:
            owner = f"create:{uuid.uuid4().hex}"
            _reserve_build(owner)
            try:
                busy = _pending_payload("create_workflow")
                if busy is not None:
                    return busy
                client = _get_client()
                workflow_id = client.create_workflow() if name is None else client.create_workflow(name=name)
                return {"ok": True, "workflow_id": workflow_id,
                    "action_required": None,
                    "next_actions": ["start_new_workflow", "start_build_workflow_json"]}
            finally:
                _release_build(owner)
        return _tool_call("create_workflow", _impl)

    @flowx_mcp.tool()
    def start_new_workflow(workflow_id: str, prompt: str, cascade: bool = True,
                           fastapi_port: int = 8000, smoke_test: bool = False,
                           serve: bool = False, interactive: bool = True) -> str:
        """Start building an empty workflow allocated by create_workflow; poll get_build_status.

        An interactive test failure is exposed as input_required only after the engine pauses.
        """
        if not str(prompt or "").strip():
            return _error("start_new_workflow", "prompt must be a non-empty string")
        return _tool_call("start_new_workflow", lambda: _start_background(
            workflow_id, "build_new_workflow", workflow_id, prompt, cascade=cascade,
            fastapi_port=fastapi_port, smoke_test_main=smoke_test, serve=serve,
            await_repair_input=interactive,
        ))

    @flowx_mcp.tool()
    def start_build_workflow_json(workflow_id: str, prompt: str, temperature: float = 0.35) -> str:
        """Start creating or modifying a workflow graph and requirements; poll get_build_status.

        Use an empty workflow allocated by create_workflow, or an existing complete workflow.
        Nodes and the main entrypoint are not rebuilt; use start_build_nodes and
        start_build_main_entrypoint afterwards when needed. Partial records cannot be overwritten.
        """
        text = str(prompt or "").strip()
        if not text:
            return _error("start_build_workflow_json", "prompt must be a non-empty string")
        return _tool_call("start_build_workflow_json", lambda: _start_background(
            workflow_id, "build_allocated_workflow_json", workflow_id, text, temperature=temperature,
        ))

    @flowx_mcp.tool()
    def start_build_main_entrypoint(workflow_id: str, amendment: str = "", fastapi_port: int = 8000,
                                    smoke_test: bool = False) -> str:
        """Start creating or regenerating the main entrypoint; poll get_build_status.

        The workflow must have built nodes. Amendment describes optional changes,
        fastapi_port selects the service port, and smoke_test audits a run before acceptance.
        """
        return _tool_call("start_build_main_entrypoint", lambda: _start_background(
            workflow_id, "build_main_entrypoint", workflow_id, amendment,
            fastapi_port=fastapi_port, smoke_test_main=smoke_test,
        ))

    @flowx_mcp.tool()
    def start_build_nodes(workflow_id: str, amendments: Optional[dict[str, str]] = None,
                          cascade: bool = False, interactive: bool = True) -> str:
        """Start building existing nodes without waiting; poll get_build_status(workflow_id)."""
        return _tool_call("start_build_nodes", lambda: _start_background(
            workflow_id, "build_nodes", workflow_id, dict(amendments) if amendments else None,
            cascade=cascade, await_repair_input=interactive,
        ))

    @flowx_mcp.tool()
    def start_build_workflow(workflow_id: str, prompt: str = "", logs: Optional[list[str]] = None,
                             fix_errors: bool = False, cascade: bool = True,
                             fastapi_port: int = 8000, smoke_test: bool = False,
                             serve: bool = False, interactive: bool = True) -> str:
        """Start changing an existing workflow in the background; poll get_build_status."""
        if not (str(prompt or "").strip() or logs or fix_errors):
            return _error("start_build_workflow", "provide a prompt, logs, or fix_errors=true")
        return _tool_call("start_build_workflow", lambda: _start_background(
            workflow_id, "build_workflow", prompt or "", workflow_id=workflow_id,
            logs=tuple(logs or ()), fix_errors=fix_errors, cascade=cascade,
            fastapi_port=fastapi_port, smoke_test_main=smoke_test, serve=serve,
            await_repair_input=interactive,
        ))

    @flowx_mcp.tool()
    def start_evolve_workflow(workflow_id: str, command: str, target: Optional[str] = None,
                              cascade: bool = True, fastapi_port: int = 8000,
                              smoke_test: bool = False, serve: bool = False,
                              interactive: bool = True) -> str:
        """Start evolving an existing workflow in the background; poll get_build_status."""
        if not str(command or "").strip():
            return _error("start_evolve_workflow", "command must be a non-empty string")
        return _tool_call("start_evolve_workflow", lambda: _start_background(
            workflow_id, "evolve", workflow_id, command, target=target or None,
            cascade=cascade, fastapi_port=fastapi_port, smoke_test_main=smoke_test,
            serve=serve, await_repair_input=interactive,
        ))

    @flowx_mcp.tool()
    def get_build_status(workflow_id: str) -> str:
        """Get the latest background build observation without waiting for the engine.

        If input_required, show awaiting.prompt and awaiting.test_log to the user and
        submit their answer with the exact revision, request_id and awaiting.stepId.
        State is in memory and is lost when the MCP server process exits.
        """
        def _impl() -> dict[str, Any]:
            with _BUILD_LOCK:
                job = _BUILD_JOBS.get(workflow_id)
                if job is None:
                    raise LookupError(f"no background build for workflow {workflow_id} in this server process")
                return _job_snapshot(job)
        return _status_call("get_build_status", _impl)

    @flowx_mcp.tool()
    def submit_background_build_input(workflow_id: str, revision: int, request_id: str,
                                      step_id: str, input: str = "") -> str:
        """Accept a user answer and resume in the background; poll get_build_status again.

        Supply the exact revision, request_id and awaiting.stepId from the latest observation.
        'test: guidance' amends the test; 'skip' skips this amendment; other text amends the node.
        """
        return _status_call("submit_background_build_input", lambda: _resume_background(
            workflow_id, revision, request_id, step_id, input,
        ))

    @flowx_mcp.tool()
    def recover_background_build(workflow_id: str, revision: int) -> str:
        """Recheck an uncertain build against the engine; never start a replacement build first."""
        def _impl() -> dict[str, Any]:
            with _BUILD_LOCK:
                job = _BUILD_JOBS.get(workflow_id)
                if (job is None or job["status"] != "recovery_required"
                        or job["revision"] != revision or _BUILD_ACTIVE != workflow_id):
                    raise ValueError("stale or non-recoverable build; call get_build_status again")
                job["status"] = "recovering"
                _job_action(job)
            try:
                state = _check_build_recovery(workflow_id)
                pending = state if state.get("status") == "input_required" else None
            except Exception:
                with _BUILD_LOCK:
                    job["status"] = "recovery_required"
                    _job_action(job)
                raise
            with _BUILD_LOCK:
                job["revision"] += 1
                if state.get("status") == "cleanup_required":
                    job.update(status="recovery_required", error="native candidate cleanup is still required")
                elif pending is None:
                    job.update(status="failed", request_id=None, awaiting=None)
                    _release_build(workflow_id)
                else:
                    job.update(status="input_required", request_id=pending["request_id"],
                               awaiting=pending["awaiting"])
                _job_action(job)
                return _job_snapshot(job)
        return _tool_call("recover_background_build", _impl)

    @flowx_mcp.tool()
    def abort_background_build(workflow_id: str, revision: int, request_id: str) -> str:
        """Abort an awaiting background build using its exact revision and request_id."""
        def _impl() -> dict[str, Any]:
            with _BUILD_LOCK:
                job = _BUILD_JOBS.get(workflow_id)
                if (job is None or job["status"] != "input_required" or job["revision"] != revision
                        or job["request_id"] != request_id or _BUILD_ACTIVE != workflow_id):
                    raise ValueError("stale or non-paused build; call get_build_status again")
                job["status"] = "aborting"
                _job_action(job)
            try:
                _get_client().abort(request_id)
            except Exception:
                with _BUILD_LOCK:
                    job["status"] = "recovery_required"
                    job["revision"] += 1
                    _job_action(job)
                raise
            with _BUILD_LOCK:
                job.update(status="aborted", revision=revision + 1, request_id=None,
                           awaiting=None, result=None, error=None)
                _job_action(job)
                _release_build(workflow_id)
                return _job_snapshot(job)
        return _tool_call("abort_background_build", _impl)

    @flowx_mcp.tool()
    def list_pending_builds() -> str:
        """List the active background build and any question it waits on.

        This only reports jobs registered in this server process without waiting for the
        engine. Use get_build_status for details, submit_background_build_input to answer,
        or abort_background_build to abort a paused build. State is lost on server exit.
        """

        def _impl() -> dict[str, Any]:
            with _BUILD_LOCK:
                job = _BUILD_JOBS.get(_BUILD_ACTIVE) if _BUILD_ACTIVE is not None else None
                if job is not None:
                    pending = ([{"request_id": job["request_id"], "workflow_id": job["workflow_id"],
                                 "awaiting": copy.deepcopy(job["awaiting"]), "revision": job["revision"]}]
                               if job["status"] == "input_required" else [])
                    return {"ok": True, "count": len(pending), "pending": pending,
                            "action_required": job["action_required"], "status": job["status"],
                            "revision": job["revision"], "active_workflow_id": job["workflow_id"]}
                return {"ok": True, "count": 0, "pending": [], "action_required": None,
                        "active_workflow_id": None}

        return _status_call("list_pending_builds", _impl)

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
        timeout: float = 1200.0,
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

        Use a step_id or node_id as the target of start_evolve_workflow.

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
        # Closing the engine while a worker holds its run lock can hang indefinitely.
        # An active daemon is abandoned at process exit; never close its pools underneath it.
        with _BUILD_LOCK:
            active_job = _BUILD_JOBS.get(_BUILD_ACTIVE) if _BUILD_ACTIVE else None
            worker_running = _BUILD_ACTIVE is not None and (
                active_job is None or active_job["status"] not in ("input_required", "recovery_required")
            )
        if worker_running:
            logger.warning("MCP server stopped while a build is running; its outcome is unknown")
            with _client_lock:
                client = _client
            if client is not None:
                try:
                    client.stop_all_services()
                except Exception:
                    logger.exception("could not stop workflow services during shutdown")
        else:
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
