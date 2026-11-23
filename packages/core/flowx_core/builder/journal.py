# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Build journal and the state board an agent reads.

Every change to a build goes through :meth:`BuildBoard.emit`: the event is appended to
``build/events.jsonl`` and then applied to the in-memory state, so the journal and the
snapshot an agent sees can never disagree.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

STAGE_NAMES = ("requirement", "graph", "nodes", "entrypoint")
DONE_NODE_STATUSES = frozenset({"published", "reused", "skipped"})


class BuildJournal:
    """Append-only JSON-lines log of build events with a monotonic sequence number."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._seq = 0
        self._revision = 0
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                    self._seq = max(self._seq, int(record.get("seq", 0)))
                    self._revision = max(self._revision, int(record.get("revision", 0)))
                except (ValueError, TypeError, AttributeError):
                    continue

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def last_revision(self) -> int:
        return self._revision

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": round(time.time(), 3), **event}
            self._revision = max(self._revision, int(record.get("revision", 0) or 0))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            return record

    def read(self, since: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and int(record.get("seq", 0)) > since:
                records.append(record)
                if len(records) >= limit:
                    break
        return records


class BuildBoard:
    """The live state of one build revision, reduced from its events."""

    def __init__(self, workflow_id: str, revision: int, journal: BuildJournal) -> None:
        self.workflow_id = workflow_id
        self.revision = revision
        self.journal = journal
        self._lock = RLock()
        self.status = "running"  # running | completed | failed | aborted
        self.phase = "starting"
        self.error = ""
        self.summary: dict[str, Any] = {}
        self.stages: dict[str, dict[str, Any]] = {name: {"name": name, "status": "pending"} for name in STAGE_NAMES}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}

    # -- writes --------------------------------------------------------------------------------
    def emit(self, type: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            event = self.journal.append({"workflow_id": self.workflow_id, "revision": self.revision, "type": type, **data})
            self.apply(event)
            return event

    def apply(self, event: Mapping[str, Any]) -> None:
        kind = event["type"]
        if kind == "stage":
            self.stages.setdefault(event["stage"], {"name": event["stage"]}).update(
                status=event["status"], **({"detail": event["detail"]} if event.get("detail") else {}),
            )
        elif kind == "phase":
            self.phase = event["phase"]
        elif kind == "node":
            node = self.nodes.setdefault(event["step_id"], {"step_id": event["step_id"], "status": "blocked"})
            node.update({key: value for key, value in event.items()
                         if key not in ("seq", "ts", "workflow_id", "revision", "type")})
        elif kind == "task_issued":
            self.tasks[event["task"]["task_id"]] = dict(event["task"])
        elif kind == "task_closed":
            self.tasks.pop(event["task_id"], None)
        elif kind == "build_finished":
            self.status = event["status"]
            self.error = event.get("error", "")
            self.summary = dict(event.get("summary", {}))
            self.tasks.clear()

    # -- reads ---------------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            nodes = [dict(node) for node in self.nodes.values()]
            total = len(nodes)
            done = sum(1 for node in nodes if node["status"] in DONE_NODE_STATUSES)
            stages = []
            for stage in self.stages.values():
                entry = dict(stage)
                if entry["name"] == "nodes" and total:
                    entry.update(done=done, total=total)
                stages.append(entry)
            tasks = list(self.tasks.values())
            return {
                "workflow_id": self.workflow_id,
                "revision": self.revision,
                "status": self.status,
                "phase": self.phase,
                "stages": stages,
                "nodes": nodes,
                "tasks": tasks,
                "error": self.error,
                "summary": dict(self.summary),
                "next_action": self.next_action(tasks),
                "last_event_seq": self.journal.last_seq,
            }

    def next_action(self, tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        tasks = list(self.tasks.values()) if tasks is None else tasks
        if self.status == "running":
            if tasks:
                return {"kind": "submit_task", "task_ids": [task["task_id"] for task in tasks]}
            return {"kind": "wait"}
        if self.status == "completed":
            return {"kind": "run_service"}
        if self.status == "failed":
            return {"kind": "escalate_to_user", "reason": self.error}
        return {"kind": "begin_build"}


__all__ = ["BuildBoard", "BuildJournal", "DONE_NODE_STATUSES", "STAGE_NAMES"]
