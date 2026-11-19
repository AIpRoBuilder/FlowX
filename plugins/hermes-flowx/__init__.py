# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: register read-only FlowX agent tools with Hermes.

"""Hermes agent plugin: read-only FlowX workflow inspection tools."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE_NAME = "flowx_hermes_service"


def _load_service() -> Any:
    existing = sys.modules.get(_SERVICE_NAME)
    if existing is not None:
        return existing
    try:  # Loaded as a package directory by Hermes.
        from . import flowx_service as service  # type: ignore[import-not-found]
        sys.modules[_SERVICE_NAME] = service
    except Exception:
        path = Path(__file__).resolve().with_name("flowx_service.py")
        spec = importlib.util.spec_from_file_location(_SERVICE_NAME, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("FlowX service module is unavailable")
        service = importlib.util.module_from_spec(spec)
        sys.modules[_SERVICE_NAME] = service
        spec.loader.exec_module(service)
    return service


service = _load_service()
MAX_TOOL_TEXT = 200_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _truncate(value: Any) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= MAX_TOOL_TEXT:
        return text, False
    return text[:MAX_TOOL_TEXT] + "\n… [truncated]", True


def _tool_error(exc: Any) -> str:
    return _json(
        {
            "ok": False,
            "error": {
                "code": getattr(exc, "code", "flowx/internal"),
                "message": getattr(exc, "message", "The operation failed."),
            },
        }
    )


def _safe_call(action: Any, params: dict[str, Any]) -> str:
    try:
        return _json({"ok": True, **action(params)})
    except service.FlowXError as exc:
        return _tool_error(exc)
    except Exception:  # Never leak paths or tracebacks through the model-facing tool.
        logger.debug("FlowX tool failed", exc_info=True)
        return _json({"ok": False, "error": {"code": "flowx/internal", "message": "The operation failed."}})


def _list_workflows(_params: dict[str, Any]) -> dict[str, Any]:
    settings = service.load_settings()
    return {
        "workspace": str(settings.workspace),
        "workspace_exists": settings.workspace_exists,
        "workflows": service.list_workflows(),
    }


def _get_workflow(params: dict[str, Any]) -> dict[str, Any]:
    workflow_id = params.get("workflow_id")
    workflow = service.get_workflow(workflow_id)
    if params.get("include_requirement") and workflow.get("requirement_path"):
        requirement = service.read_requirement(workflow_id)
        content, truncated = _truncate(requirement.get("content", ""))
        workflow["requirement"] = content
        workflow["requirement_truncated"] = truncated
    return {"workflow": workflow}


def _list_runs(params: dict[str, Any]) -> dict[str, Any]:
    return service.list_runs(params.get("workflow_id"))


def _get_run(params: dict[str, Any]) -> dict[str, Any]:
    workflow_id = params.get("workflow_id")
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        runs = service.list_runs(workflow_id)
        run_id = str(runs.get("latestRunId") or "")
        if not run_id:
            return {"run": None, "message": "This workflow has no recorded runs."}
    return {"run": service.get_run(workflow_id, run_id)}


def _read_file(params: dict[str, Any]) -> dict[str, Any]:
    result = service.read_text(params.get("workflow_id"), params.get("path"))
    content, truncated = _truncate(result.get("content", ""))
    return {**result, "content": content, "truncated": truncated}


_WORKFLOW_ID = {
    "type": "string",
    "description": "Workflow folder id under <workspace>/workflows, as returned by flowx_list_workflows.",
}
_PATH = {
    "type": "string",
    "description": "POSIX path relative to the workflow directory, e.g. plan/workflow.json or runs/<id>/run.json.",
}

_TOOLS = (
    (
        "flowx_list_workflows",
        "List FlowX workflows in the configured workspace with versions, node counts, errors, and the latest run status.",
        {"type": "object", "properties": {}},
        _list_workflows,
    ),
    (
        "flowx_get_workflow",
        "Read one FlowX workflow's plan graph, node artifacts, main entrypoint path, errors, and optional requirement analysis.",
        {
            "type": "object",
            "properties": {
                "workflow_id": _WORKFLOW_ID,
                "include_requirement": {
                    "type": "boolean",
                    "description": "Include plan/requirement_analysis.md content (bounded to 1 MiB). Default: false.",
                },
            },
            "required": ["workflow_id"],
        },
        _get_workflow,
    ),
    (
        "flowx_list_runs",
        "List recent FlowX run journals for one workflow, newest first.",
        {"type": "object", "properties": {"workflow_id": _WORKFLOW_ID}, "required": ["workflow_id"]},
        _list_runs,
    ),
    (
        "flowx_get_run",
        "Read one FlowX run journal, including per-step status and cards. Omit run_id for the latest run.",
        {
            "type": "object",
            "properties": {
                "workflow_id": _WORKFLOW_ID,
                "run_id": {"type": "string", "description": "Run id under runs/; omit for the latest run."},
            },
            "required": ["workflow_id"],
        },
        _get_run,
    ),
    (
        "flowx_read_file",
        "Read one bounded UTF-8 text artifact from a FlowX workflow (plan, requirement, node source/test/log, run journal).",
        {
            "type": "object",
            "properties": {"workflow_id": _WORKFLOW_ID, "path": _PATH},
            "required": ["workflow_id", "path"],
        },
        _read_file,
    ),
)


def register(ctx: Any) -> None:
    """Register the five read-only FlowX tools as the ``flowx`` toolset."""

    for name, description, parameters, action in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="flowx",
            schema={"name": name, "description": description, "parameters": parameters},
            handler=lambda params, _action=action, **_: _safe_call(_action, params or {}),
            emoji="🧩",
        )


__all__ = ["register"]
