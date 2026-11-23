# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""The workflow builder: a deterministic pipeline a host coding agent drives.

FlowX no longer writes code. A build is a native ag_ui_workflow run whose gate steps
pause and hand the agent a contract packet; the agent returns artifacts, FlowX audits
and tests them, publishes versions and renders the service entrypoint. The agent sees
the whole build through ``get_build_state``/``get_build_events`` and acts only through
``submit_task``.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from flowx_core.builder.contracts import ContractPacket, parse_task_id
from flowx_core.builder.gates import (
    COMPILE_STEP,
    GRAPH_STEP,
    NODES_EXIT_STEP,
    REQUIREMENT_STEP,
    RUN_KEY,
    BuildContext,
    BuildSettings,
    CompileEntrypoint,
    GraphGate,
    NodesExit,
    RequirementGate,
    gate_step,
    node_gate_class,
)
from flowx_core.builder.journal import BuildBoard, BuildJournal
from flowx_core.builder.store import WorkflowRecord, WorkflowStore
from flowx_core.builder.verify import topological_order, validate_graph_structure
from flowx_core.tools.workflow_engine import FlowXWorkflowEngine, init_workflow_engine

_SETTLE_SECONDS = 30.0
_STAGES = frozenset({"requirement", "graph", "nodes", "entrypoint"})
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAME = re.compile(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?")


class BuildInProgress(RuntimeError):
    """A workflow already has an open build revision."""


class _Session:
    """One build revision in flight: its context, board and current native engine."""

    def __init__(self, ctx: BuildContext, board: BuildBoard, head: bool) -> None:
        self.ctx, self.board = ctx, board
        self.phase = "head" if head else "body"
        self.engine: FlowXWorkflowEngine | None = None
        self.request_id = ""
        self.lock = RLock()


class WorkflowBuilder:
    """Create, build and describe workflows; one build revision per workflow at a time."""

    def __init__(
        self,
        *,
        storage_dir: str | Path,
        settings: BuildSettings | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir).expanduser().resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings or BuildSettings()
        self._registry = RLock()
        self._records: dict[str, WorkflowRecord] = {}
        self._stores: dict[str, WorkflowStore] = {}
        self._sessions: dict[str, _Session] = {}
        self._journals: dict[str, BuildJournal] = {}
        self._closed = False
        for marker in sorted(self.storage_dir.glob("*/agent.json")):
            self._register(WorkflowRecord.load(marker.parent))

    # -- registry ---------------------------------------------------------------------------
    def _register(self, record: WorkflowRecord) -> None:
        with self._registry:
            self._records[record.workflow_id] = record
            self._stores[record.workflow_id] = WorkflowStore(record)

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        with self._registry:
            return tuple(self._records)

    def record(self, workflow_id: str) -> WorkflowRecord:
        with self._registry:
            if workflow_id not in self._records:
                raise KeyError(f"unknown workflow_id: {workflow_id}")
            return self._records[workflow_id]

    def store(self, workflow_id: str) -> WorkflowStore:
        self.record(workflow_id)
        with self._registry:
            return self._stores[workflow_id]

    def _journal(self, workflow_id: str) -> BuildJournal:
        with self._registry:
            if workflow_id not in self._journals:
                path = self.store(workflow_id).directory / "build" / "events.jsonl"
                self._journals[workflow_id] = BuildJournal(path)
            return self._journals[workflow_id]

    def create_workflow(self, name: str | None = None, brief: str = "") -> dict[str, Any]:
        """Reserve a folder and identifier for a new workflow; never reuse an existing path."""
        if name is not None and (
            not isinstance(name, str) or not name or name != name.strip() or name in {".", ".."}
            or name.endswith(".") or _UNSAFE_NAME.search(name) or _RESERVED_NAME.fullmatch(name)
        ):
            raise ValueError("workflow name must be a safe single folder name")
        new_id = name if name is not None else uuid.uuid4().hex
        with self._registry:
            if self._closed:
                raise RuntimeError("workflow builder is closed")
            if new_id in self._records:
                raise FileExistsError(f"workflow already exists: {new_id}")
            directory = self.storage_dir / new_id
            # mkdir is exclusive, including for files and symlinks, and protects other data.
            directory.mkdir()
            try:
                record = WorkflowRecord(new_id, directory, main_entrypoint_id=uuid.uuid4().hex)
                record.save()
                self._register(record)
                if brief.strip():
                    self._stores[new_id].write_brief(brief.strip())
            except BaseException:
                self._records.pop(new_id, None)
                self._stores.pop(new_id, None)
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return {"workflow_id": new_id, "storage_dir": str(directory)}

    def catalog(self) -> dict[str, dict[str, Any]]:
        """Ids, versions and unresolved errors of every workflow."""
        result = {}
        for workflow_id in self.workflow_ids:
            record, store = self.record(workflow_id), self.store(workflow_id)
            ids = store.node_ids()
            main = record.main_entrypoint_path
            result[workflow_id] = {
                "nodes": {name: ids.get(name) for name in store.enabled_nodes()} or ids,
                "workflow_version": store.workflow_version(),
                "main_entrypoint_id": record.main_entrypoint_id,
                "has_graph": record.graph_path.is_file(),
                "has_main": bool(main and main.is_file()),
                "errors": dict(record.errors),
            }
        return result

    def artifacts(self, workflow_id: str) -> dict[str, Any]:
        """Locations of a workflow's requirement, graph, node versions and main entrypoint."""
        record, store = self.record(workflow_id), self.store(workflow_id)
        version = store.latest_workflow()
        main = record.main_entrypoint_path
        return {
            "workflow_id": workflow_id,
            "workflow_version": version["version"] if version else 0,
            "requirement": str(record.requirement_path) if record.requirement_path.is_file() else None,
            "graph": str(record.graph_path) if record.graph_path.is_file() else None,
            "nodes": {
                name: {
                    "node_id": node["node_id"], "version": node["version"],
                    "artifacts": {key: str(path) for key, path in store.artifacts_of(node).items()},
                }
                for name, node in (version["nodes"].items() if version else ())
            },
            "main_entrypoint": {
                "id": record.main_entrypoint_id, "version": record.main_entrypoint_version,
                "path": str(main) if main and main.is_file() else None,
            },
        }

    def list_nodes(self, workflow_id: str) -> list[dict[str, Any]]:
        """The workflow's enabled nodes as ``{step_id, node_id, version}``; ``node_id`` is None until built."""
        store = self.store(workflow_id)
        workflow = store.latest_workflow()
        published = workflow["nodes"] if workflow else {}
        names = store.enabled_nodes() or list(published)
        return [
            {
                "step_id": name,
                "node_id": published[name]["node_id"] if name in published else None,
                "version": published[name]["version"] if name in published else 0,
            }
            for name in names
        ]

    # -- build lifecycle --------------------------------------------------------------------
    def begin_build(
        self,
        workflow_id: str,
        intent: str = "",
        *,
        target: str | None = None,
        stages: Iterable[str] | None = None,
        runtime_evidence: Any = None,
        fastapi_host: str | None = None,
        fastapi_port: int | None = None,
        rebuild_entrypoint: bool = False,
        smoke_test: bool = False,
    ) -> dict[str, Any]:
        """Open a build revision and return its state with the first tasks.

        ``target`` is ``None`` (build whatever is missing or stale), ``"workflow"`` (redo the
        requirement and graph) or a node name or id (rewrite that node).  ``stages`` picks the
        planning stages explicitly: any of ``requirement``, ``graph``, ``nodes``, ``entrypoint``.
        """
        record, store = self.record(workflow_id), self.store(workflow_id)
        with self._registry:
            if self._closed:
                raise RuntimeError("workflow builder is closed")
            existing = self._sessions.get(workflow_id)
            if existing is not None and existing.board.status == "running":
                raise BuildInProgress(
                    f"workflow {workflow_id} has an open build (revision {existing.board.revision}); "
                    "submit its tasks or abort it first"
                )
            asked, forced = self._plan(store, intent, target, stages)
            if "entrypoint" in (stages or ()) or (
                fastapi_port is not None and int(fastapi_port) != record.fastapi_port
            ) or (fastapi_host is not None and fastapi_host != record.fastapi_host):
                rebuild_entrypoint = True
            if smoke_test and self.settings.smoke is None:
                raise ValueError("smoke_test needs a service manager; create one with ServiceManager(builder)")
            if "requirement" in asked and not (intent.strip() or store.read_brief().strip()):
                raise ValueError("an intent is required to write the requirement analysis")
            if intent.strip() and not store.read_brief().strip():
                store.write_brief(intent.strip())
            journal = self._journal(workflow_id)
            board = BuildBoard(workflow_id, journal.last_revision + 1, journal)
            ctx = BuildContext(
                store=store, board=board, settings=self.settings, intent=intent.strip(),
                asked=frozenset(asked), force_nodes=frozenset(forced), runtime_evidence=runtime_evidence,
                fastapi_host=fastapi_host or record.fastapi_host,
                fastapi_port=int(fastapi_port if fastapi_port is not None else record.fastapi_port),
                rebuild_entrypoint=rebuild_entrypoint or smoke_test, smoke_test=smoke_test,
            )
            session = _Session(ctx, board, head=bool(asked))
            self._sessions[workflow_id] = session
        with session.lock:
            board.emit("build_started", intent=intent.strip()[:500], target=target, asked=sorted(asked),
                       forced=sorted(forced))
            try:
                if asked:
                    board.emit("phase", phase="planning")
                    for stage in ("requirement", "graph"):
                        if stage not in asked:
                            board.emit("stage", stage=stage, status="skipped")
                    result = self._start(session, self._head_engine(workflow_id), "head")
                else:
                    for stage in ("requirement", "graph"):
                        board.emit("stage", stage=stage, status="skipped")
                    result = self._start_body(session)
                self._drive(session, result)
            except BaseException as exc:
                self._fail(session, f"{type(exc).__name__}: {exc}")
                raise
        return {**self.get_build_state(workflow_id), "next_tasks": self.get_tasks(workflow_id)}

    def get_build_state(self, workflow_id: str) -> dict[str, Any]:
        """The board an agent polls: stages, node statuses, open tasks and the next action."""
        record = self.record(workflow_id)
        session = self._sessions.get(workflow_id)
        if session is not None:
            state = session.board.snapshot()
        else:
            journal = self._journal(workflow_id)
            finished = {event["revision"] for event in journal.read(0, 10**9) if event["type"] == "build_finished"}
            last = journal.last_revision
            state = {
                "workflow_id": workflow_id, "revision": last,
                "status": "interrupted" if last and last not in finished else "idle",
                "phase": "idle", "stages": [], "nodes": [], "tasks": [], "error": "", "summary": {},
                "next_action": {"kind": "begin_build"}, "last_event_seq": journal.last_seq,
            }
        state["errors"] = dict(record.errors)
        return state

    def get_tasks(self, workflow_id: str, *, compact: bool = False) -> list[dict[str, Any]]:
        """Full contract packets of the open tasks (``compact`` drops repeated contract text)."""
        session = self._sessions.get(workflow_id)
        if session is None or session.board.status != "running":
            return []
        packets = session.ctx.open_packets()
        return [self._view(packet, compact) for packet in packets]

    def submit_task(self, workflow_id: str, task_id: str, artifacts: Mapping[str, Any]) -> dict[str, Any]:
        """Check one task's artifacts; accepted ones are published, rejected ones come back with findings."""
        self.record(workflow_id)
        session = self._sessions.get(workflow_id)
        if session is None or session.board.status != "running":
            return {"accepted": False, "error": "no_active_build", "state": self.get_build_state(workflow_id)}
        with session.lock:
            parsed = parse_task_id(task_id)
            packet = session.ctx.packets.get(task_id)
            if parsed is None or packet is None:
                return {
                    "accepted": False, "error": "stale_task" if parsed else "malformed_task_id",
                    "message": "The task is not open; use the open tasks below.",
                    "next_tasks": self.get_tasks(workflow_id, compact=True),
                    "state": self.get_build_state(workflow_id),
                }
            session.ctx.results.pop(task_id, None)
            step = gate_step(packet.stage, packet.step_id)
            engine = session.engine
            if engine is None:
                return {"accepted": False, "error": "no_active_build", "state": self.get_build_state(workflow_id)}
            try:
                result = self._settle(session, engine.resume_steps(
                    session.request_id, step, {"task_id": task_id, "artifacts": dict(artifacts)},
                ))
                self._drive(session, result)
            except BaseException as exc:
                self._fail(session, f"{type(exc).__name__}: {exc}")
                raise
            verdict = session.ctx.results.pop(task_id, None)
            state = self.get_build_state(workflow_id)
        response: dict[str, Any] = {"task_id": task_id, "accepted": bool(verdict and verdict["accepted"])}
        if verdict is not None:
            response.update(findings=verdict["findings"], test=verdict["test"], published=verdict["published"])
        else:
            response.update(error="task_not_processed", message=session.board.error or "The build ended before the task ran.")
        response["next_tasks"] = self.get_tasks(workflow_id, compact=True)
        response["state"] = state
        return response

    def get_build_events(self, workflow_id: str, since: int = 0, limit: int = 200) -> dict[str, Any]:
        """Journal events after ``since`` (a ``seq``), oldest first."""
        journal = self._journal(workflow_id)
        events = journal.read(since, max(1, min(int(limit), 1000)))
        return {"events": events, "last_seq": events[-1]["seq"] if events else since}

    def reopen_stage(
        self, workflow_id: str, stage: str, step_id: str | None = None, reason: str = "", **options: Any,
    ) -> dict[str, Any]:
        """Start a new revision that redoes one stage: ``requirement`` (with graph), ``graph`` or a node."""
        if stage == "node":
            if not step_id:
                raise ValueError("step_id is required to reopen a node")
            return self.begin_build(workflow_id, reason, target=step_id, **options)
        if stage == "requirement":
            return self.begin_build(workflow_id, reason, stages=("requirement", "graph"), **options)
        if stage == "graph":
            return self.begin_build(workflow_id, reason, stages=("graph",), **options)
        if stage == "entrypoint":
            return self.begin_build(workflow_id, reason, stages=("entrypoint",), **options)
        raise ValueError("stage must be one of: requirement, graph, node, entrypoint")

    def abort_build(self, workflow_id: str, reason: str = "") -> dict[str, Any]:
        """Cancel the open revision; published node versions are kept."""
        self.record(workflow_id)
        session = self._sessions.get(workflow_id)
        if session is not None and session.board.status == "running":
            session.ctx.cancelled.set()
            self._release_engine(session)
            session.board.emit("build_finished", status="aborted", error=reason or "aborted", summary={})
        return self.get_build_state(workflow_id)

    def build_recovery_state(self, workflow_id: str) -> dict[str, Any]:
        """Report whether a build waits for the agent."""
        state = self.get_build_state(workflow_id)
        if state["status"] == "running" and state["tasks"]:
            return {"status": "input_required", "workflow_id": workflow_id, "tasks": state["tasks"]}
        return {"status": "clear", "workflow_id": workflow_id}

    def close(self) -> None:
        with self._registry:
            self._closed = True
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.board.status == "running":
                self.abort_build(session.board.workflow_id, "builder closed")
            self._release_engine(session)

    # -- planning ---------------------------------------------------------------------------
    def _plan(
        self, store: WorkflowStore, intent: str, target: str | None, stages: Iterable[str] | None,
    ) -> tuple[set[str], set[str]]:
        """Which planning stages the agent must write and which nodes are forced."""
        graph = store.read_graph()
        asked: set[str] = set()
        forced: set[str] = set()
        if stages is not None:
            requested = set(stages)
            if not requested <= _STAGES:
                raise ValueError(f"unknown stages {sorted(requested - _STAGES)}; use {sorted(_STAGES)}")
            asked = requested & {"requirement", "graph"}
        elif target == "workflow":
            asked = {"requirement", "graph"}
        elif target not in (None, "resume"):
            ids = store.node_ids()
            names = {name for name in store.enabled_nodes()}
            by_id = {node_id: name for name, node_id in ids.items()}
            name = target if target in names else by_id.get(target)
            if name is None or name not in names:
                raise ValueError(f"unknown target {target!r}: expected 'workflow' or a node name or id")
            forced.add(name)
        graph_ok = graph is not None and not validate_graph_structure(graph)
        if not graph_ok:
            asked.add("graph")
            if stages is None and target is None and not store.read_requirement():
                asked.add("requirement")
        return asked, forced

    # -- engine plumbing --------------------------------------------------------------------
    def _head_engine(self, workflow_id: str) -> FlowXWorkflowEngine:
        classes = {REQUIREMENT_STEP: RequirementGate, GRAPH_STEP: GraphGate}
        nodes = [{"name": step, "depends": list(cls.DEPENDENCIES)} for step, cls in classes.items()]
        return init_workflow_engine(classes, {"nodes": nodes}, graph_config_id=f"flowx-build-{workflow_id}-plan")

    def _body_engine(self, session: _Session) -> FlowXWorkflowEngine:
        ctx = session.ctx
        graph = ctx.graph()
        order = topological_order(graph)
        ctx.node_order = order
        raw = {node["name"]: node for node in graph["nodes"]}
        classes: dict[str, Any] = {}
        nodes: list[dict[str, Any]] = []
        for name in order:
            parents = [dep for dep in raw[name].get("depends", []) if dep in order]
            cls = node_gate_class(name, parents)
            classes[cls.STEP_ID] = cls
            nodes.append({"name": cls.STEP_ID, "depends": list(cls.DEPENDENCIES)})
        referenced = {dep for node in nodes for dep in node["depends"]}
        leaves = [node["name"] for node in nodes if node["name"] not in referenced]
        exit_class = type("ScopedNodesExit", (NodesExit,), {"DEPENDENCIES": leaves, "__module__": __name__})
        classes[NODES_EXIT_STEP] = exit_class
        classes[COMPILE_STEP] = CompileEntrypoint
        nodes.append({"name": NODES_EXIT_STEP, "depends": leaves})
        nodes.append({"name": COMPILE_STEP, "depends": [NODES_EXIT_STEP]})
        return init_workflow_engine(
            classes, {"nodes": nodes}, graph_config_id=f"flowx-build-{ctx.workflow_id}-nodes",
        )

    def _start(self, session: _Session, engine: FlowXWorkflowEngine, phase: str) -> Any:
        self._release_engine(session)
        session.engine, session.phase = engine, phase
        session.request_id = f"{session.ctx.workflow_id}:r{session.ctx.revision}:{phase}"
        return self._settle(session, engine.start_steps(
            request_id=session.request_id, state={RUN_KEY: session.ctx},
        ))

    def _start_body(self, session: _Session) -> Any:
        ctx, board = session.ctx, session.board
        engine = self._body_engine(session)
        board.emit("phase", phase="nodes")
        board.emit("stage", stage="nodes", status="active")
        for name in ctx.node_order:
            board.emit("node", step_id=name, status="blocked", waiting_on=ctx.parents(name))
        return self._start(session, engine, "body")

    def _settle(self, session: _Session, result: Any) -> Any:
        """Observe the run until every step that can move has reported; gates may still be starting."""
        deadline = time.monotonic() + _SETTLE_SECONDS
        engine = session.engine
        while engine is not None and result.status == "awaiting_input":
            done = set(result.outputs)
            pending = any(
                step["id"] not in done and result.states.get(step["id"]) != "awaiting_input"
                and all(dep in done for dep in step["dependencies"])
                for step in engine.steps_meta
            )
            if not pending or time.monotonic() > deadline:
                break
            time.sleep(0.02)
            result = engine.run(session.request_id, {}, resume=True)
        return result

    def _drive(self, session: _Session, result: Any) -> None:
        """Move the build forward after a run settled: next phase, completion or failure."""
        while True:
            if session.ctx.cancelled.is_set() or session.board.status != "running":
                return
            if result.status == "awaiting_input":
                return
            if result.status == "failed":
                self._fail(session, result.error or f"step {result.failed_step} failed")
                return
            if session.phase == "head":
                failed = [
                    (step, output.derived.get("error") or output.derived.get("status"))
                    for step, output in result.outputs.items()
                    if output.derived.get("status") in ("failed", "blocked", "aborted")
                ]
                self._release_engine(session)
                if failed:
                    self._fail(session, "planning failed: " + "; ".join(f"{step}: {error}" for step, error in failed))
                    return
                result = self._start_body(session)
                continue
            self._finish(session, result)
            return

    def _finish(self, session: _Session, result: Any) -> None:
        outputs = result.outputs
        nodes_out = outputs[NODES_EXIT_STEP].derived
        main_out = outputs[COMPILE_STEP].derived
        summary = {
            "workflow_version": nodes_out.get("workflow_version", 0),
            "changed_nodes": nodes_out.get("changed_nodes", []),
            "main_entrypoint_path": main_out.get("main_entrypoint_path"),
        }
        error = ""
        if nodes_out.get("status") != "ok":
            failed = nodes_out.get("failed", {})
            error = "nodes failed: " + "; ".join(f"{name}: {text}" for name, text in failed.items())
        elif main_out.get("status") == "failed":
            error = "entrypoint failed: " + str(main_out.get("error", ""))
        self._release_engine(session)
        session.board.emit("build_finished", status="failed" if error else "completed", error=error, summary=summary)
        if not error:
            shutil.rmtree(session.ctx.scratch, ignore_errors=True)

    def _fail(self, session: _Session, message: str) -> None:
        if session.board.status == "running":
            self._release_engine(session)
            session.board.emit("build_finished", status="failed", error=message, summary={})

    @staticmethod
    def _release_engine(session: _Session) -> None:
        engine, session.engine = session.engine, None
        if engine is not None and not engine.closed:
            try:
                engine.close(session.request_id)
            finally:
                engine.close()

    @staticmethod
    def _view(packet: ContractPacket, compact: bool) -> dict[str, Any]:
        data = packet.to_dict()
        if compact and packet.attempt > 1:
            data["contract"] = {
                "version": packet.contract.get("version"), "stage": packet.contract.get("stage"),
                "rules": packet.contract.get("rules"),
                "note": "Unchanged since attempt 1; call get_tasks for the full contract.",
            }
        return data


__all__ = ["BuildInProgress", "WorkflowBuilder"]
