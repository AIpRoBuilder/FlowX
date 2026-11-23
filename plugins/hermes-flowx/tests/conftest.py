# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: shared workspace fixtures for the Hermes FlowX plugin tests.

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "flowx_service.py"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def service(monkeypatch, tmp_path):
    module = load_module("flowx_hermes_service_test", SERVICE_PATH)
    monkeypatch.setattr(module, "_read_hermes_plugin_settings", lambda: {})
    monkeypatch.setenv("FLOWX_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("FLOWX_DEFAULT_WORKSPACE", raising=False)
    monkeypatch.delenv("FLOWX_PYTHON", raising=False)
    return module


@pytest.fixture()
def workspace(service, tmp_path):
    workflow = tmp_path / "workflows" / "demo"
    (workflow / "plan").mkdir(parents=True)
    (workflow / "nodes" / "n1" / "00000001" / "tests").mkdir(parents=True)
    (workflow / "nodes" / "n1" / "00000001" / "logs").mkdir(parents=True)
    (workflow / "service" / "service_abc").mkdir(parents=True)
    (workflow / "runs" / "run-1" / "media" / "A").mkdir(parents=True)

    (workflow / "plan" / "workflow.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "A",
                        "type": "WorkflowStepNode",
                        "desc": "First step",
                        "meta_node_kind": "WorkflowStepNode",
                        "ext_data": {"type": "none", "desc": "no need for ext data"},
                        "enable": True,
                    },
                    {
                        "name": "B",
                        "type": "WorkflowStepNode",
                        "desc": "Second step",
                        "meta_node_kind": "WorkflowStepNode",
                        "depends": ["A"],
                        "enable": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (workflow / "plan" / "requirement_analysis.md").write_text(
        "# Demo\nUse key sk-abcdefghijklmnopqrstuvwx for testing.\n", encoding="utf-8"
    )
    (workflow / "agent.json").write_text(
        json.dumps(
            {
                "workflow_id": "demo",
                "main_entrypoint_id": "abc",
                "main_entrypoint_version": 1,
                "main_workflow_version": 3,
                "main_entrypoint_path": str(workflow / "service" / "service_abc" / "main_entrypoint.py"),
                "errors": {"nodes/A": "failed with token=super-secret"},
            }
        ),
        encoding="utf-8",
    )
    (workflow / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflow_id": "demo",
                "workflow_version": 3,
                "workflows": [
                    {
                        "nodes": {
                            "A": {
                                "node_id": "n1",
                                "version": 1,
                                "artifacts": {
                                    "source": "nodes/n1/00000001/A.py",
                                    "test": "nodes/n1/00000001/tests/test_A.py",
                                    "log": "nodes/n1/00000001/logs/A_test.log",
                                },
                            }
                        }
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (workflow / "nodes" / "n1" / "00000001" / "A.py").write_text("print('A')\n", encoding="utf-8")
    (workflow / "nodes" / "n1" / "00000001" / "tests" / "test_A.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8"
    )
    (workflow / "nodes" / "n1" / "00000001" / "logs" / "A_test.log").write_text("ok\n", encoding="utf-8")
    (workflow / "service" / "service_abc" / "main_entrypoint.py").write_text(
        "print('service')\n", encoding="utf-8"
    )
    (workflow / "runs" / "latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "updated_at": "2026-11-19T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (workflow / "runs" / "run-1" / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "run-1",
                "workflow_version": 3,
                "status": "completed",
                "created_at": "2026-11-19T00:00:00+00:00",
                "updated_at": "2026-11-19T00:01:00+00:00",
                "awaiting": None,
                "error": "token=super-secret",
                "nodes": {
                    "A": {
                        "status": "completed",
                        "title": "A",
                        "card": {
                            "kind": "image",
                            "media": "runs/run-1/media/A/out.png",
                            "prompt": "generate sk-abcdefghijklmnopqrstuvwx",
                            "api_key": "super-secret",
                        },
                        "updated_at": "2026-11-19T00:01:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (workflow / "runs" / "run-1" / "media" / "A" / "out.png").write_bytes(PNG)
    (workflow / ".env").write_text("FLOWX_IMAGE_API_KEY=super-secret\n", encoding="utf-8")
    (workflow / "token.txt").write_text("super-secret\n", encoding="utf-8")
    return tmp_path
