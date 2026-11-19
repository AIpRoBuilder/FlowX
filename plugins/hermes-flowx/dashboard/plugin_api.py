# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: FastAPI routes for the read-only FlowX Hermes dashboard tab.

"""Dashboard backend routes mounted under ``/api/plugins/flowx/``.

Hermes imports this file as user plugin code. The shared service module lives one
level up, so it is loaded by path instead of relying on package-relative imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

_SERVICE_NAME = "flowx_hermes_service"


def _load_service() -> Any:
    existing = sys.modules.get(_SERVICE_NAME)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent.parent / "flowx_service.py"
    spec = importlib.util.spec_from_file_location(_SERVICE_NAME, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("FlowX service module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SERVICE_NAME] = module
    spec.loader.exec_module(module)
    return module


service = _load_service()
router = APIRouter()


def _fail(exc: Any) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status", 400),
        detail={"code": getattr(exc, "code", "flowx/error"), "message": getattr(exc, "message", str(exc))},
    )


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "flowx"}


@router.get("/")
def index() -> dict[str, Any]:
    try:
        return service.config_info()
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows")
def workflows() -> dict[str, Any]:
    try:
        return {**service.config_info(), "workflows": service.list_workflows()}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}")
def workflow(workflow_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "workflow": service.get_workflow(workflow_id)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/requirement")
def requirement(workflow_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, **service.read_requirement(workflow_id)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/runs")
def runs(workflow_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, **service.list_runs(workflow_id)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/runs/{run_id}")
def run(workflow_id: str, run_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "run": service.get_run(workflow_id, run_id)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/layout")
def layout(workflow_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, **service.get_layout(workflow_id)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.put("/workflows/{workflow_id}/layout")
def save_layout(workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        positions = body.get("positions") if isinstance(body, dict) else None
        return {"ok": True, **service.save_layout(workflow_id, positions)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/files")
def files(workflow_id: str, path: str = "") -> dict[str, Any]:
    try:
        return {"ok": True, **service.list_files(workflow_id, path)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.get("/workflows/{workflow_id}/file")
def file(workflow_id: str, path: str = "") -> dict[str, Any]:
    try:
        return {"ok": True, **service.read_text(workflow_id, path)}
    except service.FlowXError as exc:
        raise _fail(exc) from exc


@router.api_route("/workflows/{workflow_id}/media", methods=["GET", "HEAD"])
def media(workflow_id: str, request: Request, path: str = "") -> Response:
    try:
        status, headers, body = service.read_media(
            workflow_id,
            path,
            method=request.method,
            range_header=request.headers.get("range"),
        )
    except service.FlowXError as exc:
        failed = _fail(exc)
        detail = failed.detail if isinstance(failed.detail, dict) else {"message": str(failed.detail)}
        return Response(
            content=str(detail.get("message") or "unavailable").encode("utf-8"),
            status_code=failed.status_code,
            headers={"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"},
        )
    response = Response(content=body, status_code=status, headers=headers)
    if request.method.upper() == "HEAD":
        response.headers["content-length"] = headers.get("content-length", "0")
    return response
