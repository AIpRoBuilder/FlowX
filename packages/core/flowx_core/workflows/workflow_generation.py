# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""User-confirmed build pipeline: one gate after each generation stage.

``AgentWorkflowEngine`` runs ``reformat_request -> graph_plan`` and then hands the
nodes stage to ``DynamicWorkflowOrchestrator``, whose expanded graph also runs
``main_entrypoint_generation``. A gate can follow any of those stages. It installs
its own ``setWaitFunc`` callback (``INPUT_REQUIRED`` is UI metadata only) and
reports ``awaiting_input`` through ``WorkflowParam`` until the caller supplies an
answer:

- an empty string, ``continue``, ``ok``, ``yes``, ``approve`` or ``next`` proceeds unchanged;
- ``{"node": "<step id>", "amendment": "..."}`` amends one node of the next stage;
- any other text amends the next stage as a whole.

The gates only pause when ``AgentRequest.confirm_stages`` names their stage, so automatic
builds keep the previous single-pass behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ag_ui_workflow import StepRunOutput, WorkflowStepNode

# Stages a caller can confirm, in build order.
CONFIRM_STAGES = ("graph", "nodes", "main")
# Answers that continue without amending anything (case-insensitive, trimmed).
CONTINUE_INPUTS = frozenset({"", "continue", "ok", "okay", "yes", "approve", "next", "go"})


@dataclass(frozen=True)
class Confirmation:
    """One answer to a gate: continue as-is, amend a stage, or amend single nodes."""

    amendment: str = ""
    node_amendments: Mapping[str, str] = field(default_factory=dict)

    def apply(self, intent: Mapping[str, Any], next_stage: str) -> dict[str, Any]:
        """Fold this confirmation into the run-local intent the next stage re-parses."""
        updated = dict(intent)
        if next_stage and self.amendment:
            updated[next_stage] = {"mode": "modify", "instruction": self.amendment}
        if self.node_amendments:
            merged = dict(updated.get("node_amendments") or {})
            merged.update(self.node_amendments)
            updated["node_amendments"] = merged
        return updated


def parse_confirmation(value: Any) -> Confirmation:
    """Read one gate answer; unrecognised JSON falls back to a plain stage amendment."""
    text = str(value or "").strip()
    if text.lower() in CONTINUE_INPUTS:
        return Confirmation()
    payload: Any = None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
    if isinstance(payload, dict):
        node = payload.get("node") or payload.get("step_id")
        amendment = payload.get("amendment") or payload.get("instruction") or ""
        if isinstance(node, str) and node.strip() and isinstance(amendment, str) and amendment.strip():
            return Confirmation(node_amendments={node.strip(): amendment.strip()})
        if isinstance(amendment, str) and amendment.strip():
            return Confirmation(amendment=amendment.strip())
    return Confirmation(amendment=text)


def _stage_record(stage: str, confirmation: Confirmation) -> dict[str, Any]:
    """A ``StageOutcome``-shaped mapping so the engine can rebuild it without importing it."""
    return {
        "stage": stage,
        "status": "completed",
        "attempts": 0,
        "error": "",
        "changed": bool(confirmation.amendment or confirmation.node_amendments),
        "artifacts": {
            "confirmed_stage": stage,
            "amendment": confirmation.amendment,
            "node_amendments": dict(confirmation.node_amendments),
        },
    }


def _confirm_stages(param: Any) -> tuple[str, ...]:
    """The stages this run wants confirmed; duck-typed to keep this module engine-free."""
    request = getattr(param.state.get("agent_run"), "request", None)
    stages = getattr(request, "confirm_stages", ())
    return tuple(stages) if stages else ()


class _ConfirmStageStep(WorkflowStepNode):
    """Pause after one completed stage until the caller confirms or sends a change."""

    INPUT_REQUIRED = True
    STAGE = ""  # the stage this gate confirms
    NEXT_STAGE = ""  # the stage an amendment applies to; empty for the final gate

    def __init__(self) -> None:
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def _stage_status(self, param: Any) -> str:
        output = param.outputs.get(self.DEPENDENCIES[0])
        if output is None:
            return ""
        return str(output.derived.get("stages", {}).get(self.STAGE, {}).get("status", ""))

    def wait_function(self) -> bool:
        """Pause only for a completed, requested stage; otherwise let the gate pass through."""
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True  # a failed dependency must not strand the gate
            if self.STAGE not in _confirm_stages(param) or self._stage_status(param) != "completed":
                param.set_state(self.STEP_ID, "running")
                return True
            ready = self.STEP_ID in param.inputs  # "" is a valid "continue"
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del session_state
        context = dict(next(iter(dependency_results.values())).derived)
        confirmation = parse_confirmation(user_input)
        intent = confirmation.apply(context.get("intent") or {}, self.NEXT_STAGE)
        return StepRunOutput(
            card={
                "stage": self.STEP_ID, "status": "completed",
                "summary": f"Confirmed {self.STAGE}.",
                "amendment": confirmation.amendment,
            },
            derived={
                **context,
                "intent": intent,
                "stages": {**context.get("stages", {}), self.STEP_ID: _stage_record(self.STEP_ID, confirmation)},
            },
        )


class ConfirmGraphStep(_ConfirmStageStep):
    """Confirm workflow.json and its requirement before generating nodes."""

    STEP_ID = "confirm_graph"
    TITLE = "Confirm workflow.json"
    STAGE = "graph"
    NEXT_STAGE = "nodes"
    DEPENDENCIES = ["graph_plan"]
    PROMPT = (
        "Review the planned workflow.json and requirement analysis. Send an empty input or "
        "'continue' to build the nodes, or a change request to amend the plan first."
    )


class ConfirmNodesStep(_ConfirmStageStep):
    """Confirm the generated nodes before generating the main entrypoint."""

    STEP_ID = "confirm_nodes"
    TITLE = "Confirm nodes"
    STAGE = "nodes"
    NEXT_STAGE = "main"
    DEPENDENCIES = ["nodes_orchestrate"]
    PROMPT = (
        "Review the generated nodes, their test logs and audit results. Send an empty input or "
        "'continue' to build the main entrypoint, or a change request to amend the nodes first."
    )


class ConfirmMainStep(_ConfirmStageStep):
    """Final acknowledgement after the main entrypoint is generated."""

    STEP_ID = "confirm_main"
    TITLE = "Confirm main entrypoint"
    STAGE = "main"
    NEXT_STAGE = ""
    DEPENDENCIES = ["main_entrypoint_generation"]
    PROMPT = (
        "Review the generated main entrypoint. Send an empty input or 'continue' to finish; "
        "further changes can be requested through build_workflow or evolve_workflow."
    )


CONFIRM_STEP_CLASSES = (ConfirmGraphStep, ConfirmNodesStep, ConfirmMainStep)

__all__ = [
    "CONFIRM_STAGES",
    "CONFIRM_STEP_CLASSES",
    "CONTINUE_INPUTS",
    "ConfirmGraphStep",
    "ConfirmMainStep",
    "ConfirmNodesStep",
    "Confirmation",
    "parse_confirmation",
]
