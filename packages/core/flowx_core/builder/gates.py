# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Build steps for the ag_ui_workflow engine: agent gates plus the deterministic exit steps.

An :class:`AgentGate` is a native step whose wait function pauses the run and hands a
contract packet to the host agent. The agent's submission is checked by the same
auditors and tests the LLM pipeline used; a rejection re-issues the task with
structured findings, an acceptance publishes the artifact and lets the run continue.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Mapping

from ag_ui_workflow import StepRunOutput, WorkflowStepNode

from flowx_core.auditor import GraphJsonAuditor, MainEntryPointAuditor, NodeAuditor
from flowx_core.builder.contracts import (
    CONTRACT_VERSION,
    ContractPacket,
    graph_contract,
    make_task_id,
    node_contract,
    node_reference_text,
    requirement_contract,
    submit_spec,
)
from flowx_core.builder.entrypoint import EntrypointWriter
from flowx_core.builder.findings import Finding, finding
from flowx_core.builder.journal import BuildBoard
from flowx_core.builder.store import WorkflowRecord, WorkflowStore
from flowx_core.builder.verify import (
    Verdict,
    ancestors_of,
    blame_node,
    check_graph,
    check_node,
    check_requirement,
    parse_graph,
    stage_node,
)
from flowx_core.tools.artifact_tools import fingerprint_json, node_source_artifact, text_digest

RUN_KEY = "flowx.builder.run"
SCHEME = "flowx-agent-1"
REQUIREMENT_STEP = "requirement_gate"
GRAPH_STEP = "graph_gate"
NODES_EXIT_STEP = "nodes_exit"
COMPILE_STEP = "compile_entrypoint"
_LOG_LIMIT = 12000
_NODE_SLOTS = {"code": "source", "test": "test", "markdown": "markdown"}


def author_step(name: str) -> str:
    return f"{name}__author"


def gate_step(stage: str, task_step: str) -> str:
    """The engine step id behind a task ``(stage, step)``."""
    if stage == "requirement":
        return REQUIREMENT_STEP
    if stage == "graph":
        return GRAPH_STEP
    return author_step(task_step)


def _brief(items: list[Finding], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"rule_id": item.rule_id, "message": item.message[:300], "line": item.line}
        for item in items[:limit]
    ]


@dataclass
class BuildSettings:
    """Knobs of one builder; ``smoke`` optionally runs a produced entrypoint and reports its log."""

    max_attempts: int = 3
    require_tests: bool = True
    test_timeout: int = 120
    python_command: str = sys.executable
    run_subprocess: Callable[..., Any] = subprocess.run
    smoke: Callable[[WorkflowRecord, Path], tuple[bool, str]] | None = None


@dataclass
class GateState:
    """What one gate remembers across attempts of its task."""

    decision: str | None = None  # ask | reuse | skip | blocked | failed
    reason: str = "new"
    attempt: int = 1
    packet: ContractPacket | None = None
    accepted: bool = False
    exhausted: bool = False
    error: str = ""
    findings: list[Finding] = field(default_factory=list)
    last_test_log: str = ""
    texts: dict[str, str] = field(default_factory=dict)
    published: dict[str, Any] | None = None


class BuildContext:
    """Per-revision facts shared by every step of one build (lives in ``param.state``)."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        board: BuildBoard,
        settings: BuildSettings,
        intent: str = "",
        asked: frozenset[str] = frozenset(),
        force_nodes: frozenset[str] = frozenset(),
        runtime_evidence: Any = None,
        fastapi_host: str = "0.0.0.0",
        fastapi_port: int = 8000,
        rebuild_entrypoint: bool = False,
        smoke_test: bool = False,
    ) -> None:
        self.store, self.board, self.settings = store, board, settings
        self.workflow_id, self.revision = store.workflow_id, board.revision
        self.intent = intent
        self.asked = asked
        self.force_nodes = force_nodes
        self.runtime_evidence = runtime_evidence
        self.fastapi_host, self.fastapi_port = fastapi_host, int(fastapi_port)
        self.rebuild_entrypoint = rebuild_entrypoint
        self.smoke_test = smoke_test
        self.scratch = store.directory / "build" / f"r{board.revision}"
        self.cancelled = Event()
        self.node_order: list[str] = []
        self.changed_nodes: list[str] = []
        self.packets: dict[str, ContractPacket] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self._gates: dict[str, GateState] = {}
        self._lock = RLock()
        self._auditors: dict[str, Any] = {}

    # -- shared helpers -----------------------------------------------------------------------
    def gate(self, step_id: str) -> GateState:
        with self._lock:
            return self._gates.setdefault(step_id, GateState())

    def auditor(self, kind: str) -> Any:
        with self._lock:
            if kind not in self._auditors:
                self._auditors[kind] = {
                    "graph": GraphJsonAuditor, "node": NodeAuditor, "main": MainEntryPointAuditor,
                }[kind]()
            return self._auditors[kind]

    def graph(self) -> dict[str, Any]:
        return self.store.read_graph() or {"nodes": []}

    def raw_node(self, name: str) -> dict[str, Any]:
        for node in self.graph().get("nodes", []):
            if isinstance(node, dict) and node.get("name") == name:
                return node
        raise KeyError(f"node {name!r} is not in the graph")

    def parents(self, name: str) -> list[str]:
        return [dep for dep in self.raw_node(name).get("depends", []) if dep in self.node_order]

    def source_digest(self, name: str) -> str:
        record = self.store.latest_node(name)
        source = self.store.artifacts_of(record).get("source") if record else None
        if source is None or not source.is_file():
            raise FileNotFoundError(f"node {name} has no published source")
        return text_digest(source.read_text(encoding="utf-8"))

    def fingerprint_inputs(self, name: str) -> dict[str, Any]:
        """What a node version is a pure function of: its spec and its parents' code."""
        return {
            "scheme": SCHEME,
            "spec": fingerprint_json(self.raw_node(name)),
            "parents": {parent: self.source_digest(parent) for parent in sorted(self.parents(name))},
        }

    def issue(self, packet: ContractPacket) -> None:
        with self._lock:
            self.packets[packet.task_id] = packet
        self.board.emit("task_issued", task=packet.summary())

    def close_task(self, packet: ContractPacket, outcome: str) -> None:
        with self._lock:
            self.packets.pop(packet.task_id, None)
        self.board.emit("task_closed", task_id=packet.task_id, outcome=outcome)

    def open_packets(self) -> list[ContractPacket]:
        with self._lock:
            return list(self.packets.values())

    def ancestor_context(self, name: str) -> dict[str, Any]:
        record = self.store.latest_node(name)
        artifact = node_source_artifact(name, self.store.artifacts_of(record)["source"]) if record else {"parent_node": name}
        artifact.pop("source_path", None)
        artifact["version"] = record["version"] if record else 0
        return artifact


# -- gates -------------------------------------------------------------------------------------
class AgentGate(WorkflowStepNode):
    """A step that waits for the host agent's artifact and verifies it deterministically."""

    INPUT_REQUIRED = True
    STAGE = ""
    TASK_STEP = ""

    def __init__(self) -> None:
        super().__init__()
        self.setWaitFunc(self.wait_function)

    # -- stage-specific hooks ---------------------------------------------------------------
    def decide(self, ctx: BuildContext, gate: GateState) -> str:
        raise NotImplementedError

    def open_packet(self, ctx: BuildContext, gate: GateState) -> ContractPacket:
        raise NotImplementedError

    def check(self, ctx: BuildContext, gate: GateState, artifacts: Mapping[str, Any]) -> Verdict:
        raise NotImplementedError

    def announce(self, ctx: BuildContext, gate: GateState, status: str, **data: Any) -> None:
        """Report this gate's status on the build board."""

    def finish(self, ctx: BuildContext, gate: GateState, status: str) -> dict[str, Any]:
        """Report the final status and return extra output keys."""
        return {}

    # -- shared mechanics -------------------------------------------------------------------
    def make_packet(
        self, ctx: BuildContext, gate: GateState, *, contract: dict[str, Any],
        context: dict[str, Any], meta: dict[str, Any] | None = None,
    ) -> ContractPacket:
        context = {
            **context,
            "intent": ctx.intent,
            "last_findings": [item.to_dict() for item in gate.findings],
        }
        packet = ContractPacket(
            task_id=make_task_id(self.STAGE, self.TASK_STEP, ctx.revision, gate.attempt),
            workflow_id=ctx.workflow_id, stage=self.STAGE, step_id=self.TASK_STEP,
            revision=ctx.revision, attempt=gate.attempt, max_attempts=ctx.settings.max_attempts,
            reason=gate.reason if gate.attempt == 1 else "repair",
            contract=contract, meta=meta or {}, context=context,
            submit=submit_spec(self.STAGE, self.TASK_STEP, require_tests=ctx.settings.require_tests),
        )
        ctx.issue(packet)
        return packet

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        ctx = param.state.get(RUN_KEY)
        step = self.STEP_ID
        with param.condition:
            if ctx is None or ctx.cancelled.is_set() or not param.can_run(step, self.DEPENDENCIES):
                return True
            submission = param.inputs.pop(step, None)
        gate = ctx.gate(step)
        try:
            return self._advance(param, ctx, gate, submission)
        except Exception as exc:
            gate.decision, gate.error = "failed", f"{type(exc).__name__}: {exc}"
            if gate.packet is not None:
                ctx.close_task(gate.packet, "error")
            return True

    def _advance(self, param: Any, ctx: BuildContext, gate: GateState, submission: Any) -> bool:
        step = self.STEP_ID
        if gate.decision is None:
            param.set_state(step, "running")
            gate.decision = self.decide(ctx, gate)
        if gate.decision != "ask" or gate.accepted or gate.exhausted:
            return True
        if gate.packet is None:
            gate.packet = self.open_packet(ctx, gate)
        if submission is None:
            param.set_state(step, "awaiting_input")
            return False
        param.set_state(step, "running")
        packet = gate.packet
        artifacts = submission.get("artifacts") if isinstance(submission, Mapping) else None
        self.announce(ctx, gate, "validating")
        if not isinstance(artifacts, Mapping):
            verdict = Verdict(False, [finding("builder.bad_submission", "`artifacts` must be a mapping.")])
        else:
            verdict = self.check(ctx, gate, artifacts)
        ctx.results[packet.task_id] = verdict.to_dict()
        if verdict.accepted:
            gate.accepted, gate.published = True, verdict.published
            ctx.close_task(packet, "accepted")
            return True
        gate.findings = verdict.findings
        ctx.close_task(packet, "rejected")
        if gate.attempt >= ctx.settings.max_attempts:
            gate.exhausted = True
            return True
        gate.attempt += 1
        gate.packet = self.open_packet(ctx, gate)
        param.set_state(step, "awaiting_input")
        return False

    def process_input(self, user_input: str, dependency_results: dict[str, StepRunOutput],
                      session_state: dict[str, Any]) -> StepRunOutput:
        ctx = session_state[RUN_KEY]
        gate = ctx.gate(self.STEP_ID)
        if ctx.cancelled.is_set():
            status = "aborted"
        elif gate.decision == "failed":
            status = "failed"
        elif gate.decision == "ask":
            status = "accepted" if gate.accepted else "failed"
        else:
            status = {"skip": "skipped", "reuse": "reused", "blocked": "blocked"}[gate.decision or "blocked"]
        error = gate.error or ("; ".join(item.message[:200] for item in gate.findings[:3]) if status == "failed" else "")
        extra = self.finish(ctx, gate, status)
        derived = {"status": status, "attempts": gate.attempt, "error": error, **extra}
        return StepRunOutput(card={"status": status, "attempts": gate.attempt}, derived=derived)


class _PlanGate(AgentGate):
    """Stage reporting shared by the requirement and graph gates."""

    def announce(self, ctx: BuildContext, gate: GateState, status: str, **data: Any) -> None:
        if status == "validating":
            status, data = "active", {"detail": "validating submission"}
        ctx.board.emit("stage", stage=self.STAGE, status=status, **data)

    def finish(self, ctx: BuildContext, gate: GateState, status: str) -> dict[str, Any]:
        stage_status = {"accepted": "done", "skipped": "skipped"}.get(status, "failed")
        ctx.board.emit(
            "stage", stage=self.STAGE, status=stage_status, **({"detail": gate.error} if gate.error else {}),
        )
        return {}


class RequirementGate(_PlanGate):
    STEP_ID = REQUIREMENT_STEP
    DEPENDENCIES: list[str] = []
    STAGE = "requirement"
    TASK_STEP = "requirement"
    TITLE = "Requirement analysis"
    PROMPT = "Write the requirement analysis document for this workflow."

    def decide(self, ctx: BuildContext, gate: GateState) -> str:
        if "requirement" not in ctx.asked:
            return "skip"
        gate.reason = "modify" if ctx.store.read_requirement() else "new"
        return "ask"

    def open_packet(self, ctx: BuildContext, gate: GateState) -> ContractPacket:
        self.announce(ctx, gate, "active")
        return self.make_packet(
            ctx, gate, contract=requirement_contract(),
            context={
                "brief": ctx.store.read_brief(),
                "current_requirement": ctx.store.read_requirement(),
                "current_graph": ctx.store.read_graph(),
            },
        )

    def check(self, ctx: BuildContext, gate: GateState, artifacts: Mapping[str, Any]) -> Verdict:
        text = artifacts.get("requirement")
        extra = sorted(set(artifacts) - {"requirement"})
        findings = check_requirement(text)
        if extra:
            findings.append(finding("builder.bad_submission", f"Unknown slots: {extra}."))
        if findings:
            return Verdict(False, findings)
        ctx.store.write_requirement(text)
        return Verdict(True, published={"path": "plan/requirement_analysis.md"})


class GraphGate(_PlanGate):
    STEP_ID = GRAPH_STEP
    DEPENDENCIES = [REQUIREMENT_STEP]
    STAGE = "graph"
    TASK_STEP = "graph"
    TITLE = "Workflow graph"
    PROMPT = "Design the workflow graph (workflow.json) for this requirement."

    def decide(self, ctx: BuildContext, gate: GateState) -> str:
        param = self.get_workflow_param()
        upstream = param.outputs.get(REQUIREMENT_STEP)
        if upstream is not None and upstream.derived.get("status") in ("failed", "aborted"):
            gate.error = "requirement stage failed"
            return "blocked"
        if "graph" not in ctx.asked:
            return "skip"
        gate.reason = "modify" if ctx.store.read_graph() else "new"
        return "ask"

    def open_packet(self, ctx: BuildContext, gate: GateState) -> ContractPacket:
        self.announce(ctx, gate, "active")
        return self.make_packet(
            ctx, gate, contract=graph_contract(),
            context={
                "brief": ctx.store.read_brief(),
                "requirement": ctx.store.read_requirement(),
                "current_graph": ctx.store.read_graph(),
            },
        )

    def check(self, ctx: BuildContext, gate: GateState, artifacts: Mapping[str, Any]) -> Verdict:
        extra = sorted(set(artifacts) - {"graph"})
        graph, findings = parse_graph(artifacts.get("graph"))
        if extra:
            findings.append(finding("builder.bad_submission", f"Unknown slots: {extra}."))
        if graph is None or findings:
            return Verdict(False, findings)
        findings = check_graph(graph, ctx.scratch / "graph", ctx.auditor("graph"))
        if findings:
            return Verdict(False, findings)
        ctx.store.write_graph(graph)
        ctx.store.record.clear_errors("graph")
        ctx.store.record.save()
        names = [node["name"] for node in graph["nodes"] if node.get("enable", True) is True]
        return Verdict(True, published={"path": "plan/workflow.json", "nodes": names})

    def finish(self, ctx: BuildContext, gate: GateState, status: str) -> dict[str, Any]:
        if status == "failed":
            gate.error = gate.error or "; ".join(item.message[:200] for item in gate.findings[:3])
            ctx.store.record.errors["graph"] = gate.error
            ctx.store.record.save()
        return super().finish(ctx, gate, status)


class NodeGate(AgentGate):
    """Author one business node; subclasses are scoped per node by :func:`node_gate_class`."""

    STAGE = "node"
    NODE = ""

    def _status(self, ctx: BuildContext, status: str, **data: Any) -> None:
        ctx.board.emit("node", step_id=self.NODE, status=status, **data)

    def announce(self, ctx: BuildContext, gate: GateState, status: str, **data: Any) -> None:
        self._status(ctx, status, attempt=gate.attempt, max_attempts=ctx.settings.max_attempts, **data)

    def decide(self, ctx: BuildContext, gate: GateState) -> str:
        param = self.get_workflow_param()
        blocked = [
            dep for dep in self.DEPENDENCIES
            if param.outputs[dep].derived.get("status") in ("failed", "blocked", "aborted")
        ]
        if blocked:
            reason = "upstream failed: " + ", ".join(dep.removesuffix("__author") for dep in blocked)
            gate.error = reason
            return "blocked"
        name = self.NODE
        inputs = ctx.fingerprint_inputs(name)
        latest = ctx.store.latest_node(name)
        if name not in ctx.force_nodes and latest is not None and latest.get("scheme") != SCHEME:
            return "reuse"  # published by an earlier FlowX build; never rewritten implicitly
        if name in ctx.force_nodes:
            gate.reason = "targeted"
        elif latest is None:
            gate.reason = "new"
        elif latest.get("input_fingerprint") == fingerprint_json(inputs):
            return "reuse"
        else:
            before = latest.get("fingerprint_inputs", {})
            gate.reason = "upstream_changed" if before.get("spec") == inputs["spec"] else "spec_changed"
        if latest is not None:
            artifacts = ctx.store.artifacts_of(latest)
            for slot, key in _NODE_SLOTS.items():
                path = artifacts.get(key)
                if path is not None and path.is_file():
                    gate.texts[slot] = path.read_text(encoding="utf-8")
        return "ask"

    def open_packet(self, ctx: BuildContext, gate: GateState) -> ContractPacket:
        name = self.NODE
        graph = ctx.graph()
        raw = ctx.raw_node(name)
        parents = ctx.parents(name)
        context: dict[str, Any] = {
            "requirement": ctx.store.read_requirement(),
            "graph": graph,
            "node": raw,
            "parents": parents,
            "ancestors": [ctx.ancestor_context(item) for item in ancestors_of(graph, name) if item in ctx.node_order],
            "current": dict(gate.texts),
            "last_test_log": gate.last_test_log,
        }
        if ctx.runtime_evidence and name in ctx.force_nodes:
            context["runtime_evidence"] = ctx.runtime_evidence
        meta = {
            "node": name,
            "meta_node_kind": raw.get("meta_node_kind"),
            "ext_data": raw.get("ext_data"),
            "node_reference": node_reference_text(raw.get("meta_node_kind"), raw.get("ext_data")),
        }
        packet = self.make_packet(ctx, gate, contract=node_contract(), context=context, meta=meta)
        self.announce(
            ctx, gate, "ready" if gate.attempt == 1 else "repair_needed",
            reason=packet.reason, findings=_brief(gate.findings), task_id=packet.task_id,
        )
        return packet

    def check(self, ctx: BuildContext, gate: GateState, artifacts: Mapping[str, Any]) -> Verdict:
        name = self.NODE
        bad = [
            finding("builder.bad_submission", f"Unknown slot {slot!r}.") for slot in artifacts if slot not in _NODE_SLOTS
        ] + [
            finding("builder.bad_submission", f"Slot {slot!r} must be a string.")
            for slot, value in artifacts.items() if slot in _NODE_SLOTS and not isinstance(value, str)
        ]
        if bad:
            return Verdict(False, bad)
        gate.texts.update({slot: value for slot, value in artifacts.items()})
        graph = ctx.graph()
        directory = ctx.scratch / "nodes" / name
        targets = stage_node(
            ctx.store, directory, name, graph,
            {key: gate.texts.get(slot) for slot, key in _NODE_SLOTS.items()},
        )
        findings, test = check_node(
            name=name, raw_node=ctx.raw_node(name), targets=targets, graph_path=directory / "workflow.json",
            auditor=ctx.auditor("node"), require_tests=ctx.settings.require_tests,
            timeout=ctx.settings.test_timeout, python_command=ctx.settings.python_command,
            run_subprocess=ctx.settings.run_subprocess,
        )
        summary = {key: test[key] for key in ("ok", "returncode", "timed_out")} if test else None
        if test is not None:
            gate.last_test_log = test["log"][-_LOG_LIMIT:]
        if findings:
            return Verdict(False, findings, test=summary)
        inputs = ctx.fingerprint_inputs(name)
        source_text = targets["source"].read_text(encoding="utf-8")
        record = ctx.store.publish_node(
            name, fingerprint_json(inputs), targets,
            extra={
                "scheme": SCHEME, "fingerprint_inputs": inputs, "contract_version": CONTRACT_VERSION,
                "source_digest": text_digest(source_text),
            },
        )
        ctx.changed_nodes.append(name)
        shutil.rmtree(directory, ignore_errors=True)
        return Verdict(True, test=summary, published={"node_id": record["node_id"], "version": record["version"]})

    def finish(self, ctx: BuildContext, gate: GateState, status: str) -> dict[str, Any]:
        record = ctx.store.record
        key = f"nodes/{self.NODE}"
        latest = ctx.store.latest_node(self.NODE)
        extra: dict[str, Any] = {"node": self.NODE}
        if latest is not None:
            extra.update(node_id=latest["node_id"], version=latest["version"])
        if status in ("accepted", "reused"):
            self._status(ctx, "published" if status == "accepted" else "reused",
                         version=extra.get("version"), node_id=extra.get("node_id"))
            if status == "accepted":
                record.clear_errors(key)
                record.save()
        elif status == "blocked":
            self._status(ctx, "blocked", reason=gate.error)
        elif status == "aborted":
            self._status(ctx, "failed", reason="build aborted")
        else:
            self._status(ctx, "failed", attempt=gate.attempt, findings=_brief(gate.findings), reason=gate.error)
            record.errors[key] = "; ".join(item.message[:300] for item in gate.findings[:3]) or gate.error
            record.save()
        return extra


def node_gate_class(name: str, parents: list[str]) -> type[NodeGate]:
    """A zero-argument gate class bound to one business node and its parents."""
    return type(f"{name}Author", (NodeGate,), {
        "STEP_ID": author_step(name),
        "DEPENDENCIES": [author_step(parent) for parent in parents],
        "NODE": name,
        "TASK_STEP": name,
        "TITLE": f"Author {name}",
        "PROMPT": f"Write {name}.py and its pytest module.",
        "__module__": __name__,
    })


# -- exit steps --------------------------------------------------------------------------------
_FAILED = ("failed", "blocked", "aborted")


class NodesExit(WorkflowStepNode):
    """Join every node gate; on success pin the published node versions as a workflow snapshot."""

    STEP_ID = NODES_EXIT_STEP
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED = False
    TITLE = "Nodes complete"
    PROMPT = "Publish the workflow snapshot once every node is published."

    def process_input(self, user_input: str, dependency_results: dict[str, StepRunOutput],
                      session_state: dict[str, Any]) -> StepRunOutput:
        ctx: BuildContext = session_state[RUN_KEY]
        outputs = self.get_workflow_param().outputs
        failed = {
            name: outputs[author_step(name)].derived.get("error", "")
            for name in ctx.node_order
            if author_step(name) not in outputs or outputs[author_step(name)].derived.get("status") in _FAILED
        }
        if failed:
            ctx.board.emit("stage", stage="nodes", status="failed", detail=", ".join(sorted(failed)))
            return StepRunOutput(card={"status": "failed"}, derived={"status": "failed", "failed": failed})
        record, created = ctx.store.publish_workflow(
            ctx.node_order, ctx.graph(), ctx.store.read_requirement(), list(ctx.changed_nodes),
        )
        ctx.board.emit("stage", stage="nodes", status="done")
        ctx.board.emit("phase", phase="entrypoint")
        return StepRunOutput(
            card={"status": "ok", "workflow_version": record["version"]},
            derived={
                "status": "ok", "workflow_version": record["version"], "created": created,
                "changed_nodes": list(ctx.changed_nodes),
            },
        )


class CompileEntrypoint(WorkflowStepNode):
    """Render and audit the service entrypoint from the published node versions."""

    STEP_ID = COMPILE_STEP
    DEPENDENCIES = [NODES_EXIT_STEP]
    INPUT_REQUIRED = False
    TITLE = "Compile entrypoint"
    PROMPT = "Render the service package for the published workflow."

    def process_input(self, user_input: str, dependency_results: dict[str, StepRunOutput],
                      session_state: dict[str, Any]) -> StepRunOutput:
        ctx: BuildContext = session_state[RUN_KEY]
        record = ctx.store.record
        board = ctx.board
        upstream = dependency_results[NODES_EXIT_STEP].derived
        if upstream.get("status") != "ok":
            board.emit("stage", stage="entrypoint", status="skipped", detail="nodes did not complete")
            return StepRunOutput(card={"status": "blocked"}, derived={"status": "blocked", "error": "nodes did not complete"})
        version = ctx.store.latest_workflow()
        assert version is not None
        current = record.main_entrypoint_path is not None and record.main_entrypoint_path.is_file()
        if current and record.main_workflow_version == version["version"] and not ctx.rebuild_entrypoint:
            board.emit("stage", stage="entrypoint", status="skipped", detail="up to date")
            return StepRunOutput(card={"status": "reused"}, derived={
                "status": "reused", "workflow_version": version["version"], "main_entrypoint_path": str(record.main_entrypoint_path),
            })
        board.emit("stage", stage="entrypoint", status="active")
        try:
            path = self._render(ctx, record, version)
        except _EntrypointFailure as failure:
            record.errors[failure.key] = failure.text
            record.save()
            board.emit("stage", stage="entrypoint", status="failed", detail=failure.text[:300])
            return StepRunOutput(card={"status": "failed"}, derived={
                "status": "failed", "error": failure.text, "findings": [item.to_dict() for item in failure.findings],
            })
        record.main_entrypoint_path = path
        record.main_entrypoint_version += 1
        record.main_workflow_version = version["version"]
        record.fastapi_host, record.fastapi_port = ctx.fastapi_host, ctx.fastapi_port
        record.clear_errors("main")
        record.save()
        board.emit("stage", stage="entrypoint", status="done")
        return StepRunOutput(card={"status": "ok"}, derived={
            "status": "ok", "workflow_version": version["version"], "main_entrypoint_path": str(path),
            "main_entrypoint_version": record.main_entrypoint_version,
        })

    @staticmethod
    def _render(ctx: BuildContext, record: WorkflowRecord, version: Mapping[str, Any]) -> Path:
        store = ctx.store
        package = record.service_dir
        package.mkdir(parents=True, exist_ok=True)
        for stale in package.glob("*.py"):
            stale.unlink()
        workflow_path, _ = store.workflow_paths(version)
        shutil.copy2(workflow_path, package / "workflow.json")
        for name, node in version["nodes"].items():
            shutil.copy2(store.artifacts_of(node)["source"], package / f"{name}.py")
        try:
            path = EntrypointWriter().write(
                project_root=package, graph_json=package / "workflow.json", output=package / "main.py",
                fastapi_host=ctx.fastapi_host, fastapi_port=ctx.fastapi_port,
            )
        except (OSError, ValueError) as exc:
            raise _EntrypointFailure("main", str(exc), [finding("main.render_failed", str(exc))]) from exc
        ok, violations = ctx.auditor("main").audit_file(str(path), nodes_root=str(package))
        if not ok:
            findings = [
                finding("main.audit_failed", f"[{item.rule}] {item.detail}", file="main.py", line=item.lineno)
                for item in violations
            ]
            raise _EntrypointFailure("main", "\n".join(item.message for item in findings), findings)
        if ctx.smoke_test and ctx.settings.smoke is not None:
            record.fastapi_host, record.fastapi_port = ctx.fastapi_host, ctx.fastapi_port
            smoke_ok, text = ctx.settings.smoke(record, path)
            if not smoke_ok:
                node = blame_node(text, version["nodes"])
                findings = [finding("main.smoke_failed", text[-4000:], file=f"{node}.py" if node else "main.py")]
                raise _EntrypointFailure(f"nodes/{node}" if node else "main", text[-4000:], findings)
        return path


class _EntrypointFailure(Exception):
    def __init__(self, key: str, text: str, findings: list[Finding]) -> None:
        super().__init__(text)
        self.key, self.text, self.findings = key, text, findings


__all__ = [
    "AgentGate",
    "BuildContext",
    "BuildSettings",
    "COMPILE_STEP",
    "CompileEntrypoint",
    "GRAPH_STEP",
    "GateState",
    "GraphGate",
    "NODES_EXIT_STEP",
    "NodeGate",
    "NodesExit",
    "REQUIREMENT_STEP",
    "RUN_KEY",
    "RequirementGate",
    "SCHEME",
    "author_step",
    "gate_step",
    "node_gate_class",
]
