# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-22: cover the input-required contract loop; no LLM runs inside FlowX.
from __future__ import annotations

import pytest

import flowx_sdk
from flowx_a2a.server import _RpcError, _TaskStore, _dispatch, agent_card

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
def client(tmp_path):
    with flowx_sdk.FlowXClient(workspace=tmp_path) as instance:
        yield instance


def _start(**metadata) -> dict:
    message: dict = {"parts": [{"kind": "text", "text": "Echo the input"}]}
    if metadata:
        message["metadata"] = metadata
    return {"method": "message/send", "params": {"contextId": "context-1", "message": message}}


def _answer(task_id: str, *submissions: tuple[str, dict]) -> dict:
    parts = [{"kind": "data", "data": {"task_id": name, "artifacts": artifacts}} for name, artifacts in submissions]
    return {"method": "message/send", "params": {"message": {"taskId": task_id, "parts": parts}}}


def _open_task_ids(result: dict) -> list[str]:
    data = [part["data"] for part in result["status"]["message"]["parts"] if part["kind"] == "data"]
    return [task["task_id"] for item in data for task in item.get("tasks", [])]


def test_agent_card_advertises_a2a_protocol() -> None:
    card = agent_card("http://localhost:8001/")

    assert card["protocolVersion"] == "0.3"
    assert card["url"] == "http://localhost:8001"
    assert card["skills"][0]["id"] == "compile-workflow"


def test_a_build_is_input_required_until_the_caller_answers_every_task(client) -> None:
    store = _TaskStore()
    started = _dispatch(_start(name="echo"), client, store)

    assert started["status"]["state"] == "input-required"
    assert started["metadata"] == {"workflowId": "echo"}
    (task_id,) = _open_task_ids(started)
    assert task_id.startswith("requirement:")

    rejected = _dispatch(_answer(started["id"], (task_id, {"requirement": "# nope"})), client, store)
    assert rejected["status"]["state"] == "input-required"
    results = rejected["status"]["message"]["parts"][-1]["data"]["results"]
    assert results[0]["accepted"] is False and results[0]["findings"]

    graph = _dispatch(_answer(started["id"], (_open_task_ids(rejected)[0], {"requirement": REQUIREMENT})), client, store)
    node = _dispatch(_answer(started["id"], (_open_task_ids(graph)[0], {"graph": GRAPH})), client, store)
    assert _open_task_ids(node)[0].startswith("node:Echo:")

    done = _dispatch(_answer(started["id"], (_open_task_ids(node)[0], {"code": CODE, "test": TEST})), client, store)
    assert done["status"]["state"] == "completed"
    data = done["artifacts"][0]["parts"][0]["data"]
    assert data["workflow_version"] == 1 and data["nodes"]["Echo"]["version"] == 1
    assert _dispatch({"method": "tasks/get", "params": {"id": started["id"]}}, client, store)["status"]["state"] == "completed"
    with pytest.raises(_RpcError, match="cannot accept"):
        _dispatch(_answer(started["id"], ("x", {})), client, store)


def test_cancel_aborts_the_build(client) -> None:
    store = _TaskStore()
    started = _dispatch(_start(), client, store)

    canceled = _dispatch({"method": "tasks/cancel", "params": {"id": started["id"]}}, client, store)

    assert canceled["status"]["state"] == "canceled"
    assert client.get_build_state(started["metadata"]["workflowId"])["status"] == "aborted"


def test_an_existing_workflow_can_be_targeted_by_metadata(client) -> None:
    store = _TaskStore()
    workflow_id = client.create_workflow()
    started = _dispatch(_start(workflowId=workflow_id, stages=["requirement", "graph"]), client, store)

    assert started["metadata"]["workflowId"] == workflow_id
    assert started["status"]["state"] == "input-required"
    bad = _dispatch(_start(workflowId=workflow_id), client, store)
    assert bad["status"]["state"] == "failed" and "open build" in bad["status"]["message"]["parts"][0]["text"]


@pytest.mark.parametrize("payload, code", [
    ({"method": "tasks/get", "params": {"id": "missing"}}, -32001),
    ({"method": "nope", "params": {}}, -32601),
    ({"method": "message/send", "params": {"message": {"parts": []}}}, -32602),
    ({"method": "message/send", "params": {"message": {"taskId": "missing", "parts": []}}}, -32001),
])
def test_protocol_errors(client, payload, code) -> None:
    with pytest.raises(_RpcError) as raised:
        _dispatch(payload, client, _TaskStore())

    assert raised.value.code == code
