# SPDX-License-Identifier: Apache-2.0
"""A scripted stand-in for the coding agent that drives the FlowX builder in the examples.

FlowX sends *tasks* (contract packets); the agent returns the artifacts they ask for. A real agent reads
``task["contract"]``, ``task["context"]`` and ``task["submit"]["slots"]`` and writes the code with its model or
editor. These helpers answer the tasks of a two-node demo workflow (Greeter -> Shouter) with fixed text, so the
examples run offline. Replace ``solve`` with your own function to build a real workflow.
"""

from __future__ import annotations

from typing import Any

INTENT = "A greeting workflow: Greeter produces a greeting and Shouter returns it in upper case."

REQUIREMENT = """# Requirement analysis

## Background
Greet a user and shout the greeting.

## Goals
Produce an upper-case greeting.

## Scope
Two automatic nodes, no external services.

## Functional Details

| node id | meta_node_kind | ext_data.type | purpose | depends |
| --- | --- | --- | --- | --- |
| Greeter | WorkflowStepNode | none | Builds the greeting | - |
| Shouter | WorkflowStepNode | none | Uppercases the greeting | Greeter |
"""


def _node(name: str, depends: list[str]) -> dict[str, Any]:
    return {
        "name": name, "type": name, "meta_node_kind": "WorkflowStepNode", "desc": f"{name} node",
        "ext_data": {"type": "none", "desc": "local processing"}, "depends": depends,
    }


GRAPH = {"nodes": [_node("Greeter", []), _node("Shouter", ["Greeter"])]}


def node_code(name: str, parent: str | None = None, text: str = "hello", step_id: str | None = None) -> str:
    """Python source of one automatic node: Greeter returns ``text``, Shouter upper-cases its parent's value."""
    value = f'dependency_results["{parent}"].derived["value"].upper()' if parent else repr(text)
    return f'''from __future__ import annotations

from typing import Any

from ag_ui_workflow.nodes import WorkflowStepNode
from ag_ui_workflow.workflow_types import StepRunOutput


class {name}(WorkflowStepNode):
    STEP_ID = "{step_id or name}"
    TITLE = "{name}"
    PROMPT = "{name}"
    DEPENDENCIES = {[parent] if parent else []!r}
    INPUT_REQUIRED = False

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        value = {value}
        session_state["{name}"] = value
        return StepRunOutput(card={{"label": "{name}", "value": value}}, derived={{"value": value}})
'''


def node_test(name: str, parent: str | None = None, text: str = "hello", expected: str | None = None) -> str:
    """The node's pytest module, importing the node from its file as the contract asks."""
    given = f'{{"{parent}": StepRunOutput(card={{}}, derived={{"value": "{text}"}})}}' if parent else "{}"
    want = expected if expected is not None else (text.upper() if parent else text)
    return f'''import importlib.util
from pathlib import Path

from ag_ui_workflow.workflow_types import StepRunOutput


def _node():
    path = Path(__file__).resolve().parents[1] / "{name}.py"
    spec = importlib.util.spec_from_file_location("{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.{name}()


def test_identity():
    assert _node().STEP_ID == "{name}"


def test_behavior():
    output = _node().process_input("", {given}, {{}})
    assert output.derived["value"] == {want!r}
'''


def solve(task: dict[str, Any], greeting: str = "hello") -> dict[str, Any]:
    """Answer one task of the demo workflow."""
    stage, step = task["stage"], task["step_id"]
    if stage == "requirement":
        return {"requirement": REQUIREMENT}
    if stage == "graph":
        return {"graph": GRAPH}
    if step == "Greeter":
        return {"code": node_code("Greeter", text=greeting), "test": node_test("Greeter", text=greeting)}
    return {"code": node_code("Shouter", "Greeter"), "test": node_test("Shouter", "Greeter", text=greeting)}
