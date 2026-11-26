# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-22: test the contract protocol through the real MCP SDK and FlowX client.
# Modified 2026-11-23: cover begin_evolve, read_workflow_artifact, verify_workflow, and inline-only submit_task.
"""The MCP tools expose the workflow builder and service manager as JSON."""

from __future__ import annotations

import json
import subprocess
import sys

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import flowx_mcp.server as server_module

REQUIREMENT = """## Background
Echo.

## Goals
Echo.

## Scope
One node.

## Functional Details
| node id | meta_node_kind | ext_data.type | purpose | depends |
| --- | --- | --- | --- | --- |
| Echo | WorkflowStepNode | none | Echo | - |
"""

GRAPH = {"nodes": [{
    "name": "Echo", "type": "Echo", "meta_node_kind": "WorkflowStepNode", "desc": "Echo",
    "ext_data": {"type": "none", "desc": "local"}, "depends": [],
}]}

EXPECTED_TOOLS = {
    "create_workflow", "begin_build", "begin_evolve", "get_build_state", "get_tasks", "submit_task",
    "get_build_events", "reopen_stage", "abort_build", "list_workflows", "list_nodes", "get_workflow",
    "describe_workflow", "read_workflow_artifact", "verify_workflow", "run_service", "stop_service",
    "service_status", "run_step", "reset_session", "list_runs", "get_run", "get_evidence",
}


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWX_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(server_module, "_client", None)
    yield server_module.create_server()
    server_module._close_client()


def run(server, steps):
    """Run ``steps(call)`` inside one in-memory MCP session; ``call`` returns the tool's JSON."""
    async def exercise():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            async def call(tool, /, **arguments):
                response = await session.call_tool(tool, arguments)
                return json.loads(response.content[0].text)
            return await steps(call, session)
    return anyio.run(exercise)


def test_registers_the_builder_and_service_tools(mcp):
    async def steps(call, session):
        return {tool.name for tool in (await session.list_tools()).tools}

    assert run(mcp, steps) == EXPECTED_TOOLS


def test_contract_round_trip_over_mcp(mcp):
    async def steps(call, session):
        created = await call("create_workflow", name="demo", brief="Echo text")
        assert created == {"ok": True, "workflow_id": "demo", "next_action": "begin_build"}

        opened = await call("begin_build", workflow_id="demo", intent="Echo text")
        task = opened["next_tasks"][0]
        assert opened["ok"] and opened["status"] == "running"
        assert task["stage"] == "requirement" and task["submit"]["slots"][0]["name"] == "requirement"
        assert task["contract"]["instructions"] and task["contract"]["rules"]

        rejected = await call("submit_task", workflow_id="demo", task_id=task["task_id"], artifacts={"requirement": "# x"})
        assert rejected["accepted"] is False
        assert {item["rule_id"] for item in rejected["findings"]} == {"requirement.sections", "requirement.node_design"}
        assert rejected["next_tasks"][0]["attempt"] == 2

        accepted = await call(
            "submit_task", workflow_id="demo", task_id=rejected["next_tasks"][0]["task_id"],
            artifacts={"requirement": REQUIREMENT},
        )
        assert accepted["accepted"] is True and accepted["next_tasks"][0]["stage"] == "graph"

        graph_task = (await call("get_tasks", workflow_id="demo"))["tasks"][0]
        assert graph_task["context"]["requirement"] == REQUIREMENT
        done = await call("submit_task", workflow_id="demo", task_id=graph_task["task_id"], artifacts={"graph": GRAPH})
        assert done["next_tasks"][0]["step_id"] == "Echo"
        assert done["next_tasks"][0]["meta"]["node_reference"]

        state = await call("get_build_state", workflow_id="demo")
        assert state["next_action"]["kind"] == "submit_task"
        assert {node["step_id"]: node["status"] for node in state["nodes"]} == {"Echo": "ready"}

        events = await call("get_build_events", workflow_id="demo", since=0, limit=5)
        assert len(events["events"]) == 5 and events["last_seq"] == events["events"][-1]["seq"]

        aborted = await call("abort_build", workflow_id="demo", reason="enough")
        assert aborted["status"] == "aborted"
        assert (await call("list_workflows"))["workflows"]["demo"]["has_graph"] is True
        assert (await call("list_nodes", workflow_id="demo"))["nodes"] == [
            {"step_id": "Echo", "node_id": None, "version": 0},
        ]
        assert (await call("get_workflow", workflow_id="demo"))["workflow"]["workflow_version"] == 0
        described = await call("describe_workflow", workflow_id="demo")
        assert described["ok"] is True
        assert described["description"] == "Echo text"
        assert described["nodes"] == [{
            "step_id": "Echo",
            "node_id": None,
            "version": 0,
            "title": "Echo",
            "description": "Echo",
            "prompt": "local",
            "meta_node_kind": "WorkflowStepNode",
            "ext_data": {"type": "none", "desc": "local"},
            "depends": [],
            "inputs_format": {},
            "input_required": False,
            "input_mode": "automatic",
            "input_example": None,
            "input_note": "Automatic node; it runs after its dependencies.",
            "source_parses_json": None,
        }]

    run(mcp, steps)


def test_failures_are_reported_as_json_not_raised(mcp):
    async def steps(call, session):
        missing = await call("get_build_state", workflow_id="nope")
        assert missing["ok"] is False and missing["tool"] == "get_build_state" and "unknown workflow_id" in missing["error"]
        assert missing["error_type"] == "KeyError"

        await call("create_workflow", name="demo")
        no_intent = await call("begin_build", workflow_id="demo")
        assert no_intent["ok"] is False and "intent is required" in no_intent["error"]
        no_main = await call("run_service", workflow_id="demo")
        assert no_main["ok"] is False and "no main entrypoint" in no_main["error"]
        not_running = await call("run_step", workflow_id="demo", step_id="Echo")
        assert not_running["ok"] is False and "not running" in not_running["error"]
        unsafe = await call("create_workflow", name="../escape")
        assert unsafe["ok"] is False

    run(mcp, steps)


def test_native_output_stays_off_the_protocol_stream():
    program = (
        "import os, sys\n"
        "from flowx_mcp.server import _protect_protocol_stream\n"
        "_protect_protocol_stream()\n"
        "os.write(1, b'native runtime line\\n')\n"
        "sys.stdout.write('protocol line\\n')\n"
        "sys.stdout.flush()\n"
    )
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60)

    assert result.stdout == "protocol line\n"
    assert "native runtime line" in result.stderr


def test_mcp_verify_read_evolve_and_inline_only_submit(mcp, tmp_path):
    async def steps(call, session):
        await call("create_workflow", name="demo", brief="Echo text")
        opened = await call("begin_build", workflow_id="demo", intent="Echo text")
        req_task = opened["next_tasks"][0]

        # File path wrapper and non-string slots are rejected by MCP submit_task.
        file_attempt = await call(
            "submit_task", workflow_id="demo", task_id=req_task["task_id"],
            artifacts={"requirement": {"file": str(tmp_path / "req.md")}},
        )
        assert file_attempt["ok"] is False and "inline" in file_attempt["error"]

        non_str = await call(
            "submit_task", workflow_id="demo", task_id=req_task["task_id"],
            artifacts={"requirement": ["not", "a", "string"]},
        )
        assert non_str["ok"] is False and "string" in non_str["error"]

        # Valid requirement + graph + clean verify + read_workflow_artifact.
        req_done = await call(
            "submit_task", workflow_id="demo", task_id=req_task["task_id"],
            artifacts={"requirement": REQUIREMENT},
        )
        assert req_done["accepted"] is True
        graph_task = req_done["next_tasks"][0]
        await call("submit_task", workflow_id="demo", task_id=graph_task["task_id"], artifacts={"graph": GRAPH})

        read_graph = await call("read_workflow_artifact", workflow_id="demo", slot="graph")
        assert read_graph["ok"] is True and read_graph["read_only"] is True and "Echo" in read_graph["content"]

        clean = await call("verify_workflow", workflow_id="demo")
        assert clean["ok"] is True and clean["modified"] == []

        # Tampering with plan/workflow.json is caught by verify_workflow and blocks begin_evolve.
        await call("abort_build", workflow_id="demo")
        (tmp_path / "workflows" / "demo" / "plan" / "workflow.json").write_text('{"nodes": []}', encoding="utf-8")
        dirty = await call("verify_workflow", workflow_id="demo")
        # verify_workflow returns payload dict with its own "ok": False, so check modified directly.
        assert dirty["modified"] and dirty["modified"][0]["path"] == "plan/workflow.json"
        blocked = await call("begin_evolve", workflow_id="demo", intent="change", scope="graph")
        assert blocked["ok"] is False and blocked["error_code"] == "workflow_files_modified_outside_flowx"

    run(mcp, steps)
