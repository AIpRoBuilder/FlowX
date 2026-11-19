# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: dashboard API route tests for the Hermes FlowX plugin.

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "dashboard" / "plugin_api.py"


def _load_api(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    spec = importlib.util.spec_from_file_location("flowx_hermes_api_test", API_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["flowx_hermes_api_test"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.service, "_read_hermes_plugin_settings", lambda: {})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/flowx")
    assert fastapi is not None
    return TestClient(app)


def test_dashboard_routes(service, workspace, monkeypatch):
    client = _load_api(monkeypatch)
    response = client.get("/api/plugins/flowx/workflows")
    assert response.status_code == 200
    assert response.json()["workflows"][0]["workflow_id"] == "demo"

    response = client.get("/api/plugins/flowx/workflows/demo")
    assert response.status_code == 200
    assert response.json()["workflow"]["graph"]["edges"] == [{"from": "A", "to": "B"}]

    response = client.get("/api/plugins/flowx/workflows/demo/runs")
    assert response.status_code == 200
    assert response.json()["latestRunId"] == "run-1"

    response = client.get("/api/plugins/flowx/workflows/demo/runs/run-1")
    assert response.status_code == 200
    assert response.json()["run"]["nodes"]["A"]["status"] == "completed"

    response = client.get("/api/plugins/flowx/workflows/demo/requirement")
    assert response.status_code == 200
    assert "[redacted]" in response.json()["content"]

    response = client.get("/api/plugins/flowx/workflows/demo/files", params={"path": ""})
    assert response.status_code == 200
    names = {entry["name"] for entry in response.json()["entries"]}
    assert "plan" in names and ".env" not in names


def test_dashboard_media_and_layout(service, workspace, monkeypatch):
    client = _load_api(monkeypatch)
    response = client.get(
        "/api/plugins/flowx/workflows/demo/media",
        params={"path": "runs/run-1/media/A/out.png"},
        headers={"range": "bytes=0-9"},
    )
    assert response.status_code == 206
    assert response.content == (workspace / "workflows/demo/runs/run-1/media/A/out.png").read_bytes()[:10]
    assert response.headers["content-range"].startswith("bytes 0-9/")

    response = client.head(
        "/api/plugins/flowx/workflows/demo/media",
        params={"path": "runs/run-1/media/A/out.png"},
    )
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(
        (workspace / "workflows/demo/runs/run-1/media/A/out.png").stat().st_size
    )

    response = client.put(
        "/api/plugins/flowx/workflows/demo/layout",
        json={"positions": {"A": {"x": 10, "y": 20}}},
    )
    assert response.status_code == 200
    assert response.json()["positions"]["A"] == {"x": 10, "y": 20}


def test_dashboard_rejects_unsafe_paths(service, workspace, monkeypatch):
    client = _load_api(monkeypatch)
    response = client.get(
        "/api/plugins/flowx/workflows/demo/file",
        params={"path": "../agent.json"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid-path"

    response = client.get("/api/plugins/flowx/workflows/..%2Fescape")
    assert response.status_code in {404, 400}
