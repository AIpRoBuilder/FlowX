# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: tests for the read-only FlowX Hermes service and agent tools.

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = ROOT / "__init__.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_list_workflows(service, workspace):
    items = service.list_workflows()
    assert len(items) == 1
    item = items[0]
    assert item["workflow_id"] == "demo"
    assert item["workflow_version"] == 3
    assert item["node_count"] == 1  # B is disabled
    assert item["built_node_count"] == 1
    assert item["has_main"] is True
    assert item["error_count"] == 1
    assert item["latest_run"]["run_id"] == "run-1"


def test_get_workflow_graph_and_artifacts(service, workspace):
    workflow = service.get_workflow("demo")
    graph = workflow["graph"]
    assert graph["available"] is True
    assert [node["name"] for node in graph["nodes"]] == ["A", "B"]
    assert graph["edges"] == [{"from": "A", "to": "B"}]
    assert graph["roots"] == ["A"]
    assert graph["terminal"] == ["B"]
    assert workflow["nodes"]["A"]["artifacts"]["source"] == "nodes/n1/00000001/A.py"
    assert workflow["main_entrypoint_path"] == "service/service_abc/main_entrypoint.py"
    assert workflow["errors"] == ["nodes/A"]


def test_read_text_redacts_json_and_requirement(service, workspace):
    agent = service.read_text("demo", "agent.json")
    assert agent["content"].find("super-secret") < 0
    assert json.loads(agent["content"])["errors"]["nodes/A"] == "Details withheld."

    requirement = service.read_requirement("demo")
    assert "sk-abcdefghijklmnopqrstuvwx" not in requirement["content"]
    assert "[redacted]" in requirement["content"]


def test_read_run_sanitizes_card(service, workspace):
    runs = service.list_runs("demo")
    assert runs["latestRunId"] == "run-1"
    assert runs["runs"][0]["status"] == "completed"
    run = service.get_run("demo", "run-1")
    assert run["has_error"] is True
    assert run["nodes"]["A"]["card"]["api_key"] == "[redacted]"
    assert "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(run)


def test_path_safety_and_file_listing(service, workspace):
    for bad in ["../agent.json", "/etc/passwd", ".env", "token.txt", "runs/../agent.json"]:
        with pytest.raises(service.FlowXError):
            service.read_text("demo", bad)
    listing = service.list_files("demo", "")
    names = {entry["name"] for entry in listing["entries"]}
    assert "plan" in names
    assert "agent.json" in names
    assert ".env" not in names
    assert "token.txt" not in names
    assert "runs" in names


def test_layout_round_trip(service, workspace):
    assert service.get_layout("demo") == {"positions": {}}
    saved = service.save_layout("demo", {"A": {"x": 12.4, "y": -8.6}, "B": {"x": 300, "y": 40}})
    assert saved["positions"] == {"A": {"x": 12, "y": -9}, "B": {"x": 300, "y": 40}}
    assert service.get_layout("demo")["positions"]["A"] == {"x": 12, "y": -9}
    with pytest.raises(service.FlowXError):
        service.save_layout("demo", {"A": {"x": 1e300, "y": 0}})


def test_media_range_and_head(service, workspace):
    png = (workspace / "workflows/demo/runs/run-1/media/A/out.png").read_bytes()
    status, headers, body = service.read_media("demo", "runs/run-1/media/A/out.png")
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert body == png

    status, headers, body = service.read_media(
        "demo", "runs/run-1/media/A/out.png", range_header="bytes=0-9"
    )
    assert status == 206
    assert headers["content-range"] == f"bytes 0-9/{len(png)}"
    assert body == png[:10]

    status, headers, body = service.read_media(
        "demo", "runs/run-1/media/A/out.png", method="HEAD"
    )
    assert status == 200
    assert headers["content-length"] == str(len(png))
    assert body == b""

    status, headers, _body = service.read_media(
        "demo", "runs/run-1/media/A/out.png", range_header="bytes=999-"
    )
    assert status == 416
    assert headers["content-range"] == f"bytes */{len(png)}"

    with pytest.raises(service.FlowXError):
        service.read_media("demo", "agent.json")


def test_single_workflow_workspace(service, workspace, monkeypatch):
    workflow = workspace / "workflows" / "demo"
    monkeypatch.setenv("FLOWX_WORKSPACE", str(workflow))
    items = service.list_workflows()
    assert [item["workflow_id"] for item in items] == ["demo"]
    assert service.get_workflow("demo")["workflow_id"] == "demo"


def test_agent_tool_registration(service, workspace, monkeypatch):
    class Context:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs["name"]] = kwargs

    module = _load("flowx_hermes_plugin_test", INIT_PATH)
    monkeypatch.setattr(module.service, "_read_hermes_plugin_settings", lambda: {})
    context = Context()
    module.register(context)
    assert set(context.tools) == {
        "flowx_list_workflows",
        "flowx_get_workflow",
        "flowx_list_runs",
        "flowx_get_run",
        "flowx_read_file",
    }
    handler = context.tools["flowx_get_workflow"]["handler"]
    payload = json.loads(handler({"workflow_id": "demo"}))
    assert payload["ok"] is True
    assert payload["workflow"]["workflow_id"] == "demo"
    failure = json.loads(handler({"workflow_id": "../escape"}))
    assert failure["ok"] is False
