# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Contracts and the packet an agent receives for one build task.

A contract is the single description of what an artifact must look like: its text, its
rules (the same ids the auditors report) and, where it exists, a schema. The builder
returns it inside a :class:`ContractPacket` together with the build context the agent
needs, so contracts live next to the auditors that enforce them and never in a skill.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from flowx_core.builder.findings import rules_for
from flowx_core.tools.workflow_node_reference import (
    render_workflow_node_reference,
    render_workflow_step_meta_catalog,
    resolve_workflow_node_reference,
)

# Bump when a contract's meaning changes; stored with every node version.
CONTRACT_VERSION = "2026.11.22.1"

TEXT_DIR = Path(__file__).resolve().parent / "contract_text"

GRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["name", "type", "meta_node_kind", "desc", "ext_data"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9]*$"},
                    "type": {"type": "string", "description": "identical to name"},
                    "meta_node_kind": {"type": "string"},
                    "desc": {"type": "string"},
                    "ext_data": {
                        "type": "object",
                        "required": ["type", "desc"],
                        "properties": {"type": {"type": "string"}, "desc": {"type": "string"}},
                    },
                    "inputs_format": {"type": "object"},
                    "enable": {"type": "boolean"},
                    "loop": {"const": 1},
                    "depends": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


_COMMENT = re.compile(r"\A<!--.*?-->\s*", re.DOTALL)


@lru_cache(maxsize=None)
def contract_text(name: str) -> str:
    """A contract's instructions; the leading attribution comment is for readers of the file, not the agent."""
    return _COMMENT.sub("", (TEXT_DIR / f"{name}.md").read_text(encoding="utf-8"))


def requirement_contract() -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "stage": "requirement",
        "instructions": contract_text("requirement"),
        "rules": rules_for("requirement"),
        "node_types": render_workflow_step_meta_catalog(),
    }


def graph_contract() -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "stage": "graph",
        "instructions": contract_text("graph"),
        "rules": rules_for("graph"),
        "schema": GRAPH_SCHEMA,
        "node_types": render_workflow_step_meta_catalog(),
    }


def node_contract() -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "stage": "node",
        "instructions": contract_text("node"),
        "test_instructions": contract_text("node_test"),
        "rules": rules_for("node"),
        "node_types": render_workflow_step_meta_catalog(),
    }


def node_reference_text(meta_node_kind: str | None, ext_data: Any = None) -> str:
    """The base-class reference a node of this kind must follow."""
    return render_workflow_node_reference(
        resolve_workflow_node_reference(meta_node_kind=meta_node_kind or None, ext_data=ext_data),
    )


_TASK_ID = re.compile(r"^(?P<stage>[a-z]+):(?P<step>[A-Za-z0-9_]+):r(?P<revision>\d+):a(?P<attempt>\d+)$")


def make_task_id(stage: str, step_id: str, revision: int, attempt: int) -> str:
    return f"{stage}:{step_id}:r{revision}:a{attempt}"


def parse_task_id(task_id: str) -> tuple[str, str, int, int] | None:
    """``(stage, step_id, revision, attempt)`` or ``None`` for a malformed id."""
    match = _TASK_ID.match(task_id or "")
    if match is None:
        return None
    return match["stage"], match["step"], int(match["revision"]), int(match["attempt"])


@dataclass
class ContractPacket:
    """Everything an agent needs to produce one artifact, and where to submit it."""

    task_id: str
    workflow_id: str
    stage: str
    step_id: str
    revision: int
    attempt: int
    max_attempts: int
    reason: str
    contract: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    submit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "reason": self.reason,
        }


def submit_spec(stage: str, step_id: str, *, require_tests: bool = True) -> dict[str, Any]:
    """The artifact slots a task accepts, keyed for ``submit_task(artifacts=...)``."""
    if stage == "requirement":
        return {"slots": [{"name": "requirement", "path": "plan/requirement_analysis.md", "required": True}]}
    if stage == "graph":
        return {"slots": [{"name": "graph", "path": "plan/workflow.json", "required": True,
                           "note": "a JSON object or its text"}]}
    return {"slots": [
        {"name": "code", "path": f"{step_id}.py", "required": True},
        {"name": "test", "path": f"tests/test_{step_id}.py", "required": require_tests},
        {"name": "markdown", "path": f"node_docs/{step_id}.md", "required": False},
    ], "note": "Omitted slots keep their content from the previous attempt or published version."}


__all__ = [
    "CONTRACT_VERSION",
    "ContractPacket",
    "GRAPH_SCHEMA",
    "contract_text",
    "graph_contract",
    "make_task_id",
    "node_contract",
    "node_reference_text",
    "parse_task_id",
    "requirement_contract",
    "submit_spec",
]
