# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-22: cover the contract protocol; the SDK no longer calls an LLM.
from pathlib import Path

import pytest

import flowx_sdk
from flowx_core.builder import WorkflowBuilder
from flowx_core.service import ServiceManager

REQUIREMENT = """## Background
Echo text.

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

CODE = '''from __future__ import annotations

from typing import Any

from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput


class Echo(WorkflowStepNode):
    STEP_ID = "Echo"
    TITLE = "Echo"
    PROMPT = "Echo"
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED = False

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        value = "echo"
        session_state["Echo"] = value
        return StepRunOutput(card={"label": "Echo", "value": value}, derived={"value": value})
'''

TEST = '''import importlib.util
from pathlib import Path


def _node():
    path = Path(__file__).resolve().parents[1] / "Echo.py"
    spec = importlib.util.spec_from_file_location("Echo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Echo()


def test_identity():
    assert _node().STEP_ID == "Echo"


def test_behavior():
    assert _node().process_input("", {}, {}).derived == {"value": "echo"}
'''


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("FLOWX_DEFAULT_WORKSPACE", raising=False)
    with flowx_sdk.FlowXClient(workspace=tmp_path) as instance:
        yield instance


def solve(task):
    return {
        "requirement": {"requirement": REQUIREMENT},
        "graph": {"graph": GRAPH},
        "node": {"code": CODE, "test": TEST},
    }[task["stage"]]


def test_the_two_entities_are_created_lazily_once_in_the_workspace(client, tmp_path):
    assert client._agent is None

    assert isinstance(client.builder, WorkflowBuilder)
    assert isinstance(client.services, ServiceManager)
    assert client.builder is client.agent.builder
    assert client.builder.storage_dir == tmp_path.resolve() / "workflows"


def test_workspace_falls_back_to_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWX_DEFAULT_WORKSPACE", str(tmp_path / "env"))

    assert flowx_sdk.FlowXClient().workspace == (tmp_path / "env").resolve()


def test_create_workflow_forwards_optional_name_and_brief(client, tmp_path):
    assert client.create_workflow("demo", brief="Echo text") == "demo"
    assert (Path(client.workspace) / "workflows" / "demo" / "plan" / "brief.md").read_text() == "Echo text"
    assert client.create_workflow()
    with pytest.raises(ValueError):
        client.create_workflow("../escape")


def test_build_answers_every_task_with_the_solver(client):
    workflow_id = client.create_workflow()
    seen = []

    def recording(task):
        seen.append((task["stage"], task["step_id"]))
        return solve(task)

    state = client.build(workflow_id, "Echo text", recording)

    assert state["status"] == "completed"
    assert seen == [("requirement", "requirement"), ("graph", "graph"), ("node", "Echo")]
    assert client.list_nodes(workflow_id) == [{"step_id": "Echo", "node_id": client.list_nodes(workflow_id)[0]["node_id"], "version": 1}]
    assert client.workflows()[workflow_id]["workflow_version"] == 1
    assert client.artifacts(workflow_id)["nodes"]["Echo"]["version"] == 1
    assert client.build_recovery_state(workflow_id) == {"status": "clear", "workflow_id": workflow_id}
    assert client.get_build_events(workflow_id)["events"][-1]["type"] == "build_finished"


def test_manual_protocol_round_trip_and_abort(client):
    workflow_id = client.create_workflow()
    opened = client.begin_build(workflow_id, "Echo text")

    assert opened["status"] == "running" and opened["next_tasks"][0]["stage"] == "requirement"
    assert client.build_recovery_state(workflow_id)["status"] == "input_required"
    response = client.submit_task(workflow_id, opened["next_tasks"][0]["task_id"], {"requirement": "# bad"})
    assert response["accepted"] is False and response["findings"]
    assert client.get_tasks(workflow_id, compact=True)[0]["attempt"] == 2

    assert client.abort_build(workflow_id, "stop")["status"] == "aborted"
    assert client.reopen_stage(workflow_id, "requirement", reason="again")["status"] == "running"


def test_build_gives_up_after_max_turns(client):
    workflow_id = client.create_workflow()

    with pytest.raises(RuntimeError, match="did not finish"):
        client.build(workflow_id, "Echo text", lambda task: {}, max_turns=2)


def test_services_are_idle_until_started_and_close_is_idempotent(client):
    client.stop_all_services()  # nothing created yet
    workflow_id = client.create_workflow()

    assert client.service_status(workflow_id)["running"] is False
    with pytest.raises(FileNotFoundError):
        client.run_service(workflow_id)
    client.close()
    client.close()
