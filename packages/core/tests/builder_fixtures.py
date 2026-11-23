# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Shared fixtures: a scripted stand-in for the host coding agent and sample nodes."""

from __future__ import annotations

from typing import Any

from flowx_core.builder import WorkflowBuilder

REQUIREMENT = """# Requirement analysis

## Background
Greet a user and shout the greeting.

## Goals
Produce an uppercase greeting.

## Scope
Two automatic nodes, no external services.

## Functional Details

| node id | meta_node_kind | ext_data.type | purpose | depends |
| --- | --- | --- | --- | --- |
| Greeter | WorkflowStepNode | none | Builds the greeting | - |
| Shouter | WorkflowStepNode | none | Uppercases the greeting | Greeter |
"""


def graph_of(*nodes: tuple[str, list[str]]) -> dict[str, Any]:
    return {"nodes": [
        {
            "name": name, "type": name, "meta_node_kind": "WorkflowStepNode", "desc": f"{name} node",
            "ext_data": {"type": "none", "desc": "local processing"}, "depends": depends,
        }
        for name, depends in nodes
    ]}


CHAIN = graph_of(("Greeter", []), ("Shouter", ["Greeter"]))
DIAMOND = graph_of(("Left", []), ("Right", []), ("Join", ["Left", "Right"]))


def node_code(name: str, parents: list[str] | None = None, *, text: str = "hello", step_id: str | None = None) -> str:
    parents = parents or []
    reads = "".join(
        f'        {parent.lower()} = dependency_results["{parent}"].derived["value"]\n' for parent in parents
    )
    value = " + ".join([*(parent.lower() for parent in parents), repr(text)]) if parents else repr(text)
    return (
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        "from ag_ui_workflow.nodes import WorkflowStepNode\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n\n\n"
        f"class {name}(WorkflowStepNode):\n"
        f'    STEP_ID = "{step_id or name}"\n'
        f'    TITLE = "{name}"\n'
        f'    PROMPT = "{name}"\n'
        f"    DEPENDENCIES = {parents!r}\n"
        "    INPUT_REQUIRED = False\n\n"
        "    def process_input(\n"
        "        self,\n"
        "        user_input: str,\n"
        "        dependency_results: dict[str, StepRunOutput],\n"
        "        session_state: dict[str, Any],\n"
        "    ) -> StepRunOutput:\n"
        f"{reads}"
        f"        value = {value}\n"
        f'        session_state["{name}"] = value\n'
        f'        return StepRunOutput(card={{"label": "{name}", "value": value}}, derived={{"value": value}})\n'
    )


def node_test(name: str, parents: list[str] | None = None, *, expected: str = "hello", fail: bool = False) -> str:
    parents = parents or []
    given = ", ".join(f'"{parent}": StepRunOutput(card={{}}, derived={{"value": "{parent}-"}})' for parent in parents)
    prefix = "".join(f"{parent}-" for parent in parents)
    return (
        "import importlib.util\n"
        "from pathlib import Path\n\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n\n\n"
        "def _load():\n"
        f'    path = Path(__file__).resolve().parents[1] / "{name}.py"\n'
        f'    spec = importlib.util.spec_from_file_location("{name}", path)\n'
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n"
        f"    return module.{name}\n\n\n"
        f"def test_{name.lower()}_identity():\n"
        "    node = _load()()\n"
        f'    assert node.STEP_ID == "{name}"\n\n\n'
        f"def test_{name.lower()}_behavior():\n"
        "    node = _load()()\n"
        "    state = {}\n"
        f"    output = node.process_input('', {{{given}}}, state)\n"
        f'    assert output.derived["value"] == {prefix + expected!r}{" + 1" if fail else ""}\n'
    )


def good(name: str, parents: list[str] | None = None) -> dict[str, str]:
    return {"code": node_code(name, parents), "test": node_test(name, parents)}


class Agent:
    """Answers open tasks from a script of artifacts keyed by ``stage:step``."""

    def __init__(self, builder: WorkflowBuilder, workflow_id: str, script: dict[str, list[Any]]) -> None:
        self.builder, self.workflow_id, self.script = builder, workflow_id, script
        self.responses: list[dict[str, Any]] = []
        self.seen: list[str] = []

    def step(self) -> dict[str, Any] | None:
        tasks = self.builder.get_tasks(self.workflow_id)
        if not tasks:
            return None
        task = tasks[0]
        key = f"{task['stage']}:{task['step_id']}"
        self.seen.append(f"{key}:a{task['attempt']}")
        artifacts = self.script[key].pop(0)
        artifacts = artifacts(task) if callable(artifacts) else artifacts
        response = self.builder.submit_task(self.workflow_id, task["task_id"], artifacts)
        self.responses.append(response)
        return response

    def run(self, limit: int = 30) -> None:
        for _ in range(limit):
            if self.step() is None:
                return
        raise AssertionError("build did not settle")


def plan_script(graph: dict[str, Any]) -> dict[str, list[Any]]:
    return {"requirement:requirement": [{"requirement": REQUIREMENT}], "graph:graph": [{"graph": graph}]}


