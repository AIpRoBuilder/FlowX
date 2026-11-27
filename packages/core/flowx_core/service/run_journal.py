# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Moved 2026-11-27 from flowx_core/media.py into flowx_core/service/run_journal.py;
# made DAG-aware so each workflow pass gets its own run_id and clears stale awaiting state.
"""DAG-aware per-pass filesystem run journal under `<workflow_root>/runs/`."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    if not cleaned:
        cleaned = fallback
    cleaned = cleaned[:96]
    if not _SAFE_SEGMENT.match(cleaned):
        return fallback
    return cleaned


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _step_field(item: Any, *keys: str) -> Any:
    if isinstance(item, Mapping):
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]
        return None
    for key in keys:
        value = getattr(item, key, None)
        if value is not None:
            return value
    return None


@dataclass
class _RunSnapshot:
    run_id: str
    session_id: str
    workflow_version: int
    status: str = "running"
    created_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)
    awaiting: dict[str, Any] | None = None
    error: str | None = None
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "workflow_version": self.workflow_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "awaiting": self.awaiting,
            "error": self.error,
            "nodes": self.nodes,
        }


class RunJournal:
    """Thread-safe, DAG-aware filesystem run journal writing `<workflow_root>/runs/<run_id>/run.json`."""

    def __init__(
        self,
        workflow_root: str | os.PathLike[str],
        *,
        workflow_version: int | None = None,
        steps: Sequence[Any] | None = None,
    ) -> None:
        self.workflow_root = Path(workflow_root).expanduser().resolve()
        self.runs_dir = self.workflow_root / "runs"
        self._lock = RLock()
        self._explicit_version = workflow_version
        self._snapshots: dict[str, _RunSnapshot] = {}
        self._session_runs: dict[str, str] = {}
        self.steps: dict[str, tuple[str, ...]] = {}
        self.head_steps: frozenset[str] = frozenset()
        self.tail_steps: frozenset[str] = frozenset()
        self.set_steps(steps if steps is not None else self._load_plan_steps())

    def _load_plan_steps(self) -> list[dict[str, Any]]:
        plan_path = self.workflow_root / "plan" / "workflow.json"
        try:
            if plan_path.is_file():
                raw = json.loads(plan_path.read_text(encoding="utf-8"))
                nodes = raw.get("nodes") if isinstance(raw, Mapping) else None
                if isinstance(nodes, list):
                    return [item for item in nodes if isinstance(item, Mapping)]
        except Exception:
            pass
        return []

    def set_steps(self, steps: Sequence[Any]) -> None:
        """Register the workflow DAG step map and derive head/tail step IDs."""
        step_map: dict[str, tuple[str, ...]] = {}
        for item in steps:
            if _step_field(item, "enable") is False:
                continue
            raw_id = _step_field(item, "id", "name", "step_id")
            step_id = str(raw_id or "").strip()
            if not step_id:
                continue
            raw_deps = _step_field(item, "dependencies", "depends") or ()
            if isinstance(raw_deps, (str, bytes)):
                raw_deps = (raw_deps,)
            deps = tuple(str(dep).strip() for dep in raw_deps if str(dep).strip())
            step_map[step_id] = deps
        referenced = {dep for deps in step_map.values() for dep in deps}
        with self._lock:
            self.steps = step_map
            self.head_steps = frozenset(step_id for step_id, deps in step_map.items() if not deps)
            self.tail_steps = frozenset(step_id for step_id in step_map if step_id not in referenced)

    def _detect_workflow_version(self) -> int:
        if self._explicit_version is not None:
            return int(self._explicit_version)
        manifest_path = self.workflow_root / "manifest.json"
        try:
            if manifest_path.is_file():
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                ver = data.get("workflow_version")
                if isinstance(ver, int) and not isinstance(ver, bool) and ver >= 0:
                    return ver
        except Exception:
            pass
        return 0

    def _get_existing_snapshot(self, run_id: str) -> _RunSnapshot | None:
        safe_id = _safe_segment(run_id, "run")
        snap = self._snapshots.get(safe_id)
        if snap is not None:
            return snap
        existing_file = self.runs_dir / safe_id / "run.json"
        if existing_file.is_file():
            return self._load_or_create(safe_id, "")
        return None

    def _load_or_create(self, run_id: str, session_id: str) -> _RunSnapshot:
        safe_id = _safe_segment(run_id, "run")
        snap = self._snapshots.get(safe_id)
        if snap is not None:
            if session_id:
                snap.session_id = session_id
            return snap
        existing_file = self.runs_dir / safe_id / "run.json"
        if existing_file.is_file():
            try:
                raw = json.loads(existing_file.read_text(encoding="utf-8"))
                snap = _RunSnapshot(
                    run_id=safe_id,
                    session_id=str(raw.get("session_id") or session_id),
                    workflow_version=int(raw.get("workflow_version") or self._detect_workflow_version()),
                    status=str(raw.get("status") or "running"),
                    created_at=str(raw.get("created_at") or _utc_iso()),
                    updated_at=str(raw.get("updated_at") or _utc_iso()),
                    awaiting=raw.get("awaiting") if isinstance(raw.get("awaiting"), dict) else None,
                    error=raw.get("error") if isinstance(raw.get("error"), str) else None,
                    nodes=dict(raw.get("nodes") or {}),
                )
                self._snapshots[safe_id] = snap
                return snap
            except Exception:
                pass
        snap = _RunSnapshot(
            run_id=safe_id,
            session_id=session_id,
            workflow_version=self._detect_workflow_version(),
        )
        self._snapshots[safe_id] = snap
        return snap

    def _tail_completed(self, snap: _RunSnapshot) -> bool:
        return bool(self.tail_steps) and all(
            (snap.nodes.get(step_id) or {}).get("status") == "completed"
            for step_id in self.tail_steps
        )

    def _pass_completed(self, snap: _RunSnapshot) -> bool:
        return snap.status in _TERMINAL_STATUSES or self._tail_completed(snap)

    def should_start_new_run(self, run_id: str | None, step_id: str) -> bool:
        """Return True when `(run_id, step_id)` must start a fresh workflow pass."""
        if not run_id:
            return True
        with self._lock:
            snap = self._get_existing_snapshot(run_id)
            if snap is None:
                return False
            if self._pass_completed(snap):
                return True
            if step_id in self.head_steps:
                node = snap.nodes.get(step_id) or {}
                awaiting_step = (snap.awaiting or {}).get("stepId")
                if node.get("status") == "completed" and awaiting_step != step_id:
                    return True
            return False

    def resolve_run_id(
        self,
        session_id: str,
        step_id: str,
        *,
        current_run_id: str | None = None,
        on_abandon: Callable[[str], None] | None = None,
    ) -> tuple[str, bool]:
        """Resolve `(run_id, started_new)` for one step request on `session_id`."""
        with self._lock:
            existing = current_run_id if current_run_id is not None else self._session_runs.get(session_id)
            if existing and not self.should_start_new_run(existing, step_id):
                self._session_runs[session_id] = existing
                return existing, False
            if existing:
                snap = self._get_existing_snapshot(existing)
                if snap is not None and snap.status in {"running", "input_required"}:
                    if on_abandon is not None:
                        on_abandon(existing)
                    if not self._tail_completed(snap):
                        self.mark_cancelled(existing)
            new_run_id = str(uuid.uuid4())
            self._session_runs[session_id] = new_run_id
            return new_run_id, True

    def bind_session_run(self, session_id: str, run_id: str) -> None:
        """Bind `session_id` to an explicitly rotated `run_id` (e.g. after reset-session)."""
        with self._lock:
            self._session_runs[session_id] = run_id

    def clear_sessions(self) -> None:
        """Drop in-memory session-to-run bindings (e.g. on service shutdown)."""
        with self._lock:
            self._session_runs.clear()

    def _flush(self, snap: _RunSnapshot) -> None:
        snap.updated_at = _utc_iso()
        payload = snap.to_dict()
        run_file = self.runs_dir / snap.run_id / "run.json"
        _write_json_atomic(run_file, payload)
        _write_json_atomic(
            self.runs_dir / "latest.json",
            {
                "schema_version": 1,
                "run_id": snap.run_id,
                "session_id": snap.session_id,
                "workflow_version": snap.workflow_version,
                "status": snap.status,
                "updated_at": snap.updated_at,
            },
        )

    def mark_cancelled(self, run_id: str) -> None:
        safe_id = _safe_segment(run_id, "run")
        with self._lock:
            snap = self._get_existing_snapshot(safe_id)
            if snap is None:
                return
            if snap.status in {"running", "input_required"}:
                snap.status = "cancelled"
                snap.awaiting = None
                self._flush(snap)

    def tee(self, run_id: str, session_id: str, events: Iterator[str]) -> Iterator[str]:
        """Yield SSE lines unchanged while atomically journaling per-node status and cards."""
        safe_id = _safe_segment(run_id, "run")
        with self._lock:
            snap = self._load_or_create(safe_id, session_id)
            snap.status = "running"
            snap.awaiting = None
            snap.error = None
            if session_id:
                self._session_runs[session_id] = run_id
            self._flush(snap)
        for chunk in events:
            self._ingest_chunk(safe_id, session_id, chunk)
            yield chunk

    def _ingest_chunk(self, safe_id: str, session_id: str, chunk: str) -> None:
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            with self._lock:
                snap = self._load_or_create(safe_id, session_id)
                etype = str(event.get("type") or "")
                ename = str(event.get("name") or "")
                if etype == "STEP_STARTED":
                    step_name = str(event.get("stepName") or "").strip()
                    if step_name:
                        prev = snap.nodes.get(step_name) or {}
                        snap.nodes[step_name] = {
                            **prev,
                            "status": "running",
                            "updated_at": _utc_iso(),
                        }
                        self._flush(snap)
                elif ename == "step_card" and isinstance(event.get("value"), Mapping):
                    val = event["value"]
                    step_name = str(val.get("stepId") or "").strip()
                    if step_name:
                        card = val.get("card")
                        state = str(val.get("state") or "completed")
                        snap.nodes[step_name] = {
                            "status": state,
                            "title": str(val.get("title") or step_name),
                            "card": dict(card) if isinstance(card, Mapping) else {},
                            "is_final": bool(val.get("isFinal")),
                            "updated_at": _utc_iso(),
                        }
                        if snap.awaiting and snap.awaiting.get("stepId") == step_name and state != "awaiting_input":
                            snap.awaiting = None
                        self._flush(snap)
                elif ename == "input_required" and isinstance(event.get("value"), Mapping):
                    val = event["value"]
                    step_name = str(val.get("stepId") or "").strip()
                    snap.status = "input_required"
                    snap.awaiting = {
                        "stepId": step_name,
                        "title": str(val.get("title") or ""),
                        "prompt": str(val.get("prompt") or ""),
                    }
                    if step_name:
                        prev = snap.nodes.get(step_name) or {}
                        snap.nodes[step_name] = {
                            **prev,
                            "status": "awaiting_input",
                            "updated_at": _utc_iso(),
                        }
                    self._flush(snap)
                elif etype == "RUN_ERROR":
                    snap.status = "failed"
                    snap.awaiting = None
                    snap.error = str(event.get("message") or "Workflow step failed")
                    self._flush(snap)
                elif etype == "RUN_FINISHED":
                    res = event.get("result") if isinstance(event.get("result"), Mapping) else {}
                    if res.get("ok") is False:
                        snap.status = "failed"
                        snap.awaiting = None
                        failed_step = str(res.get("stepId") or "").strip()
                        if failed_step:
                            prev = snap.nodes.get(failed_step) or {}
                            snap.nodes[failed_step] = {
                                **prev,
                                "status": "failed",
                                "updated_at": _utc_iso(),
                            }
                    elif res.get("awaitingInput"):
                        awaiting_step = str(res.get("awaitingInput") or "").strip()
                        snap.status = "input_required"
                        if snap.awaiting is None or snap.awaiting.get("stepId") != awaiting_step:
                            snap.awaiting = {
                                "stepId": awaiting_step,
                                "title": "",
                                "prompt": "",
                            }
                    else:
                        snap.status = "completed"
                        snap.awaiting = None
                    self._flush(snap)


__all__ = ["RunJournal"]
