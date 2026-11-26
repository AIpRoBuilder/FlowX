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
# Modified 2026-11-22: replace the background LLM build jobs with the contract protocol of the workflow builder
#   (begin_build/get_tasks/submit_task/...) and expose the service manager; keep native output off the stdio stream.
# Modified 2026-11-23: add begin_evolve, read_workflow_artifact, and verify_workflow; forbid direct workflow file edits.
# Modified 2026-11-26: add describe_workflow and document step input modes.
"""FastMCP server exposing the FlowX workflow builder and service manager to a coding agent."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import logging
import os
import sys
import threading
from typing import Any, Callable, Literal, Optional

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

INSTRUCTIONS = """\
FlowX builds executable AG-UI workflows, but YOU write the code: FlowX hands you contracts, audits and tests what
you submit, versions it, and renders and runs the service.

Workflow files are owned by FlowX (READ-ONLY)
- Never create, edit, delete, or overwrite files under `<workspace>/workflows/` directly (`plan/`, `nodes/`,
  `workflows/`, `manifest.json`, `agent.json`, `build/`, `service/service_<id>/`).
- To inspect file contents use `read_workflow_artifact` (read-only); `get_workflow` returns read-only locations.
- All changes go through `begin_build` or `begin_evolve`, followed by inline-text `submit_task`.
- Use `verify_workflow` to check digest integrity. If `workflow_files_modified_outside_flowx` is returned, stop and
  report the modified files; never hand-edit them back.

Build loop
1. create_workflow -> workflow_id.
2. begin_build(workflow_id, intent). The result has `next_tasks`: contract packets. A task has `contract`
   (instructions, rules, schema), `context` (requirement, graph, upstream node sources, current artifacts, last
   findings), `meta` and `submit.slots` (the artifact names you must return).
3. For each task submit inline text with submit_task(workflow_id, task_id, artifacts). Stages in order:
   requirement (Markdown) -> graph (workflow.json) -> one node task per business node (`code` + `test`).
   Independent nodes are open at the same time; answer them in any order, or in parallel.
4. A rejected submission returns `findings` ({rule_id, message, line, remediation}) and a repair task with
   attempt+1. Fix exactly those findings; omitted slots keep their previous content. After max_attempts the node is
   marked failed and its descendants blocked.
5. get_build_state shows mode, baseline_version, stages, per-node status (blocked, ready, validating, repair_needed,
   published, reused, revalidated, failed) and `next_action` (submit_task, wait, run_service, escalate_to_user,
   begin_build). get_build_events is the journal; pass the last `seq` as `since` to read only what is new. get_tasks
   re-reads open tasks after a lost context.
6. status=completed -> run_service, then run_step to drive it. status=failed -> read `errors` and the findings, then
   reopen_stage or ask the user. abort_build cancels an open build; published versions are kept.

Evolving a built workflow
- Edit one node without changing graph topology: begin_evolve(workflow_id, intent=<change>, scope="node",
  node=<name or node_id>, cascade="revalidate"|"rewrite"). Topology is frozen; compatible children auto-revalidate
  without a task, broken children re-open with findings and parent `source_diff`.
- Edit graph topology / node specs: begin_evolve(workflow_id, intent=<change>, scope="graph",
  update_requirement=False|True). Plan writes are staged until all nodes pass; aborting leaves `plan/` untouched.
- If a running service misbehaves, call get_evidence and pass it as `runtime_evidence` to begin_evolve.
Running a workflow
- Call describe_workflow before run_step: it returns the workflow purpose plus per-step description, prompt,
  inputs_format, input_mode and input_example. Use input_mode to decide between a string, JSON object or file_path.
  `WorkflowStepNode` input is normalized text; a dict without input/value/text becomes JSON text.
Build state lives in this server process and in <workspace>/workflows/<id>/build/events.jsonl; after a restart call
begin_build again: published nodes are reused.
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


def _error(tool: str, message: str, **extra: Any) -> str:
    return _json({"ok": False, "tool": tool, "error": message, **extra})


# One client (hence one builder and one service manager) per server process.
_client: Any = None
_client_lock = threading.Lock()


def _get_client() -> Any:
    global _client
    with _client_lock:
        if _client is None:
            _client = importlib.import_module("flowx_sdk").FlowXClient()
        return _client


def _close_client() -> None:
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


async def _call(tool: str, func: Callable[[], Any]) -> str:
    """Run ``func`` off the event loop: a submission may wait for a node's pytest run."""
    try:
        payload = await asyncio.to_thread(func)
        return _json({"ok": True, **payload} if isinstance(payload, dict) else {"ok": True, "result": payload})
    except Exception as exc:
        logger.exception("%s failed", tool)
        extra: dict[str, Any] = {"error_type": type(exc).__name__}
        if hasattr(exc, "modified") and hasattr(exc, "remedy"):
            extra.update(
                error_code="workflow_files_modified_outside_flowx",
                modified=getattr(exc, "modified"),
                remedy=getattr(exc, "remedy"),
            )
        return _error(tool, str(exc), **extra)


def _validate_mcp_artifacts(artifacts: Any) -> dict[str, Any]:
    """MCP submit_task accepts inline text (or a JSON object for `graph`), never file paths."""
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object mapping slot names to inline text")
    for slot, value in artifacts.items():
        if isinstance(value, dict) and set(value) == {"file"}:
            raise ValueError(
                f"MCP submit_task does not accept file paths for {slot!r}; pass the artifact text inline"
            )
        if slot != "graph" and not isinstance(value, str):
            raise ValueError(f"MCP submit_task slot {slot!r} must be inline text (string)")
    return artifacts


def create_server() -> Any:
    """Create and return the FlowX MCP server with all tools registered."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server could not load a compatible SDK entry point. "
            "Expected either 'mcp.server.fastmcp.FastMCP' or 'mcp.server.mcpserver.MCPServer'. "
            f"Install or repair the SDK with: {sys.executable} -m pip install -U 'mcp'. "
            f"Last import error: {_MCP_SERVER_IMPORT_ERROR}"
        )

    flowx_mcp = MCPServerClass("flowx", instructions=INSTRUCTIONS)

    # -- workflow builder -------------------------------------------------------------------
    @flowx_mcp.tool()
    async def create_workflow(name: Optional[str] = None, brief: str = "") -> str:
        """Allocate an empty workflow; `name`, if given, is its folder and workflow_id."""
        return await _call("create_workflow", lambda: {
            "workflow_id": _get_client().create_workflow(name, brief),
            "next_action": "begin_build",
        })

    @flowx_mcp.tool()
    async def begin_build(
        workflow_id: str,
        intent: str = "",
        target: Optional[str] = None,
        stages: Optional[list[str]] = None,
        runtime_evidence: Any = None,
        fastapi_port: Optional[int] = None,
        fastapi_host: Optional[str] = None,
        smoke_test: bool = False,
    ) -> str:
        """Open a build revision and return its state with the first contract tasks (`next_tasks`).

        intent: what to build (new workflow) or the change to make (existing one).
        target: omit to build whatever is missing or stale; "workflow" to redo requirement and graph;
            or a node name / node_id to rewrite that node. Prefer `begin_evolve` for editing built workflows.
        stages: explicit subset of requirement, graph, nodes, entrypoint (advanced).
        runtime_evidence: observations from a running service (see get_evidence) for a targeted rewrite.
        smoke_test: start the rendered service once and audit its log before accepting the entrypoint.
        Fails while the workflow already has an open build; use get_build_state or abort_build.
        """
        options: dict[str, Any] = {
            "target": target or None, "stages": stages or None, "runtime_evidence": runtime_evidence,
            "fastapi_port": fastapi_port, "fastapi_host": fastapi_host, "smoke_test": smoke_test,
        }
        return await _call("begin_build", lambda: _get_client().begin_build(workflow_id, intent, **options))

    @flowx_mcp.tool()
    async def begin_evolve(
        workflow_id: str,
        intent: str = "",
        scope: Literal["graph", "node"] = "node",
        node: Optional[str] = None,
        update_requirement: bool = False,
        cascade: Literal["revalidate", "rewrite"] = "revalidate",
        runtime_evidence: Any = None,
        fastapi_port: Optional[int] = None,
        fastapi_host: Optional[str] = None,
        smoke_test: bool = False,
    ) -> str:
        """Open an edit revision on a built workflow; never edit workflow files directly.

        scope="node" edits `node` with graph topology frozen (body engine only); compatible children
            auto-revalidate when `cascade="revalidate"`, or re-open with findings and parent diff on failure.
        scope="graph" edits `workflow.json` (and optionally requirement when `update_requirement=True`)
            with plan writes staged until all nodes pass.
        """
        options: dict[str, Any] = {
            "scope": scope, "node": node or None, "update_requirement": update_requirement,
            "cascade": cascade, "runtime_evidence": runtime_evidence,
            "fastapi_port": fastapi_port, "fastapi_host": fastapi_host, "smoke_test": smoke_test,
        }
        return await _call("begin_evolve", lambda: _get_client().begin_evolve(workflow_id, intent, **options))

    @flowx_mcp.tool()
    async def get_build_state(workflow_id: str) -> str:
        """Stages, per-node status, open task summaries, `next_action` and unresolved `errors`."""
        return await _call("get_build_state", lambda: _get_client().get_build_state(workflow_id))

    @flowx_mcp.tool()
    async def get_tasks(workflow_id: str, compact: bool = False) -> str:
        """The open contract tasks in full (`compact` omits contract text repeated from earlier attempts)."""
        return await _call("get_tasks", lambda: {"tasks": _get_client().get_tasks(workflow_id, compact=compact)})

    @flowx_mcp.tool()
    async def submit_task(workflow_id: str, task_id: str, artifacts: dict[str, Any]) -> str:
        """Submit inline text artifacts for one task: {requirement}, {graph} or {code, test, markdown?}.

        File path wrappers are rejected; always pass inline text (or a JSON object for `graph`).
        Returns `accepted`, `findings` (with remediation), the node `test` result, what was `published`,
        the new `next_tasks` and the build `state`. Blocks while the node's test runs.
        """
        return await _call(
            "submit_task",
            lambda: _get_client().submit_task(workflow_id, task_id, _validate_mcp_artifacts(artifacts)),
        )

    @flowx_mcp.tool()
    async def get_build_events(workflow_id: str, since: int = 0, limit: int = 200) -> str:
        """The build journal after sequence number `since`, oldest first."""
        return await _call("get_build_events", lambda: _get_client().get_build_events(workflow_id, since, limit))

    @flowx_mcp.tool()
    async def reopen_stage(workflow_id: str, stage: str, step_id: Optional[str] = None, reason: str = "") -> str:
        """Start a new revision that redoes one stage: requirement (with graph), graph, node (needs step_id)
        or entrypoint. `reason` is the instruction for the rewrite."""
        return await _call(
            "reopen_stage", lambda: _get_client().reopen_stage(workflow_id, stage, step_id, reason),
        )

    @flowx_mcp.tool()
    async def abort_build(workflow_id: str, reason: str = "") -> str:
        """Cancel the open build revision; published node versions are kept."""
        return await _call("abort_build", lambda: _get_client().abort_build(workflow_id, reason))

    # -- inspection -------------------------------------------------------------------------
    @flowx_mcp.tool()
    async def list_workflows() -> str:
        """Every workflow with its node ids, versions and unresolved errors."""
        return await _call("list_workflows", lambda: {"workflows": _get_client().workflows()})

    @flowx_mcp.tool()
    async def list_nodes(workflow_id: str) -> str:
        """A workflow's nodes as step_id, node_id and version (node_id is null until built)."""
        return await _call("list_nodes", lambda: {"nodes": _get_client().list_nodes(workflow_id)})

    @flowx_mcp.tool()
    async def get_workflow(workflow_id: str) -> str:
        """Read-only locations of a workflow's requirement, workflow.json, node versions and main entrypoint.
        Never write to these paths directly; use `read_workflow_artifact` to inspect and `begin_build`/`begin_evolve` to change."""
        return await _call("get_workflow", lambda: {"workflow": _get_client().artifacts(workflow_id)})

    @flowx_mcp.tool()
    async def describe_workflow(workflow_id: str) -> str:
        """Workflow purpose plus per-step descriptions, input formats, input modes and examples."""
        return await _call("describe_workflow", lambda: _get_client().describe_workflow(workflow_id))

    @flowx_mcp.tool()
    async def read_workflow_artifact(
        workflow_id: str,
        slot: Literal["requirement", "graph", "brief", "source", "test", "markdown", "log"],
        node: Optional[str] = None,
        version: Optional[int] = None,
    ) -> str:
        """Read a FlowX-owned workflow artifact as verified read-only text. Edit only via begin_build/begin_evolve."""
        return await _call(
            "read_workflow_artifact",
            lambda: _get_client().read_artifact(workflow_id, slot, node=node or None, version=version),
        )

    @flowx_mcp.tool()
    async def verify_workflow(workflow_id: str, include_service: bool = True) -> str:
        """Check that FlowX-owned files still match their recorded SHA-256 digests. Never repair mismatches by hand."""
        return await _call(
            "verify_workflow",
            lambda: _get_client().verify_workflow(workflow_id, include_service=include_service),
        )

    # -- service manager --------------------------------------------------------------------
    @flowx_mcp.tool()
    async def run_service(workflow_id: str) -> str:
        """Start the built workflow as a service (a running one is replaced) and wait until it answers."""
        return await _call("run_service", lambda: {"service": _get_client().run_service(workflow_id)})

    @flowx_mcp.tool()
    async def stop_service(workflow_id: str) -> str:
        """Stop a workflow's running service, if any."""
        return await _call("stop_service", lambda: {"service": _get_client().stop_service(workflow_id)})

    @flowx_mcp.tool()
    async def service_status(workflow_id: str) -> str:
        """Whether the service runs, its pid, base_url and log path."""
        return await _call("service_status", lambda: {"service": _get_client().service_status(workflow_id)})

    @flowx_mcp.tool()
    async def run_step(
        workflow_id: str,
        step_id: str,
        input: Any = None,
        session_id: str = "",
        file_path: Optional[str] = None,
        timeout: float = 1200.0,
    ) -> str:
        """Send one step's input to the running service; reports `input_required` with the waiting step
        (`awaiting`: stepId, title, prompt) or completion. Resume with that step id and the answer."""
        return await _call("run_step", lambda: _get_client().run_step(
            workflow_id, step_id, input, session_id=session_id, file_path=file_path, timeout=timeout,
        ))

    @flowx_mcp.tool()
    async def reset_session(workflow_id: str, session_id: str = "") -> str:
        """Cancel the session's paused run; the next run_step starts a fresh one."""
        return await _call("reset_session", lambda: _get_client().reset_session(workflow_id, session_id))

    @flowx_mcp.tool()
    async def list_runs(workflow_id: str, limit: int = 20) -> str:
        """Recent runs journaled by the service, newest first."""
        return await _call("list_runs", lambda: {"runs": _get_client().list_runs(workflow_id, limit)})

    @flowx_mcp.tool()
    async def get_run(workflow_id: str, run_id: str = "latest") -> str:
        """One run's journal: per-node status, cards and the question it waits on."""
        return await _call("get_run", lambda: {"run": _get_client().get_run(workflow_id, run_id)})

    @flowx_mcp.tool()
    async def get_evidence(workflow_id: str, run_id: str = "latest") -> str:
        """Run journal plus service log tail, to pass as `runtime_evidence` to begin_build."""
        return await _call("get_evidence", lambda: {"evidence": _get_client().evidence(workflow_id, run_id)})

    return flowx_mcp


def _protect_protocol_stream() -> None:
    """Keep the protocol on the original stdout and send everything else to stderr.

    The native workflow runtime writes to file descriptor 1 from C++, which a Python-level
    redirect cannot capture; a stray line there would corrupt the JSON-RPC stream.
    """
    try:
        sys.stdout.flush()
        protocol = os.dup(1)
        os.dup2(2, 1)
    except (OSError, ValueError):  # no usable stdout (for example under a test harness)
        return
    sys.stdout = io.TextIOWrapper(os.fdopen(protocol, "wb"), encoding="utf-8", line_buffering=True)


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
    _protect_protocol_stream()
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
        # Aborts open builds (published versions stay) and stops the services started here.
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
