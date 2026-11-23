# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: verify workflow integrity and re-render derived service package from the verified snapshot before launch.
"""Run, drive and inspect the services FlowX built."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from threading import RLock
from typing import IO, Any

from flowx_core.auditor import OutputAuditor
from flowx_core.builder import WorkflowBuilder
from flowx_core.builder.gates import render_service_package
from flowx_core.builder.store import WorkflowRecord
from flowx_core.builder_services import service_environment
from flowx_core.tools.agent_builder_tools import select_python_command

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _http(url: str, payload: dict[str, Any], timeout: float) -> str:
    """POST JSON and return the body (SSE text for ``/api/run-step``)."""
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # keep the 404/422 detail visible to the caller
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}") from exc


def _sse_payloads(text: str) -> list[dict[str, Any]]:
    """The generated service writes one ``data: {json}`` line per AG-UI event."""
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                value = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(value, dict):
                payloads.append(value)
    return payloads


def _terminate(process: Any) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            pass


def _tail(path: Path, limit: int = 6000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


class ServiceManager:
    """Start, stop and drive the services of the workflows a :class:`WorkflowBuilder` owns."""

    def __init__(self, builder: WorkflowBuilder, *, ready_timeout: float = 15.0) -> None:
        self.builder = builder
        self.ready_timeout = ready_timeout
        self._lock = RLock()
        self._logs: dict[str, IO[bytes]] = {}
        self._sessions: dict[str, str] = {}
        self._closed = False
        builder.settings.smoke = self.smoke_test

    # -- lifecycle --------------------------------------------------------------------------
    def _spawn(self, record: WorkflowRecord, main: Path, log_path: Path) -> Any:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("wb")
        try:
            process = subprocess.Popen(
                [select_python_command(), str(main)], cwd=str(main.parent),
                env=service_environment(main), stdout=handle, stderr=subprocess.STDOUT,
            )
        except BaseException:
            handle.close()
            raise
        previous = self._logs.pop(record.workflow_id, None)
        if previous is not None:
            previous.close()
        self._logs[record.workflow_id] = handle
        return process

    def _wait_ready(self, record: WorkflowRecord, process: Any, log_path: Path | None = None) -> None:
        """The process only starts; probe ``GET /`` before the first step."""
        deadline = time.monotonic() + self.ready_timeout
        while True:
            code = process.poll()
            if code is not None:
                raise RuntimeError(
                    f"workflow service exited with code {code} before it was ready; "
                    f"log tail:\n{_tail(log_path or self.log_path(record))}"
                )
            try:
                with urllib.request.urlopen(f"{record.base_url}/", timeout=1.0):
                    return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"workflow service at {record.base_url} did not become ready")
                time.sleep(0.2)

    @staticmethod
    def log_path(record: WorkflowRecord) -> Path:
        return record.storage_dir / "logs" / "service.log"

    def _running(self, record: WorkflowRecord) -> bool:
        return record.process is not None and record.process.poll() is None

    def run_service(self, workflow_id: str, *, wait: bool = True) -> dict[str, Any]:
        """Start the main entrypoint as a service, replacing a previous run of it."""
        record = self.builder.record(workflow_id)
        if record.main_entrypoint_path is None:
            raise FileNotFoundError(f"workflow {workflow_id} has no main entrypoint; build it first")
        self.builder._require_integrity(workflow_id, include_service=False)
        store = self.builder.store(workflow_id)
        version = store.latest_workflow()
        with self._lock:
            if self._closed:
                raise RuntimeError("service manager is closed")
            if version is not None:
                # Re-render the derived service package so hand-edited service files cannot run.
                main = render_service_package(
                    store, record, version, fastapi_host=record.fastapi_host, fastapi_port=record.fastapi_port,
                )
                record.main_entrypoint_path = main
                record.save()
            else:
                main = record.main_entrypoint_path
            if main is None or not main.is_file():
                raise FileNotFoundError(f"workflow {workflow_id} has no main entrypoint; build it first")
            _terminate(record.process)
            record.process = self._spawn(record, main, self.log_path(record))
            try:
                if wait:
                    self._wait_ready(record, record.process)
            except BaseException:
                _terminate(record.process)
                raise
            return {**self.status(workflow_id), "main_entrypoint_id": record.main_entrypoint_id}

    def stop_service(self, workflow_id: str) -> dict[str, Any]:
        record = self.builder.record(workflow_id)
        with self._lock:
            process, record.process = record.process, None
            _terminate(process)
            handle = self._logs.pop(workflow_id, None)
            if handle is not None:
                handle.close()
        return self.status(workflow_id)

    def status(self, workflow_id: str) -> dict[str, Any]:
        record = self.builder.record(workflow_id)
        process = record.process
        return {
            "workflow_id": workflow_id,
            "running": self._running(record),
            "pid": getattr(process, "pid", None),
            "exit_code": process.poll() if process is not None else None,
            "base_url": record.base_url,
            "main_entrypoint_path": str(record.main_entrypoint_path) if record.main_entrypoint_path else None,
            "log_path": str(self.log_path(record)),
        }

    def stop_all(self) -> None:
        for workflow_id in self.builder.workflow_ids:
            self.stop_service(workflow_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop_all()

    # -- driving ----------------------------------------------------------------------------
    def _session(self, workflow_id: str, session_id: str) -> str:
        with self._lock:
            return session_id or self._sessions.setdefault(workflow_id, uuid.uuid4().hex)

    def _base_url(self, workflow_id: str) -> str:
        record = self.builder.record(workflow_id)
        if not self._running(record):
            raise RuntimeError(f"workflow {workflow_id} is not running; call run_service first")
        return record.base_url

    def run_step(
        self, workflow_id: str, step_id: str, input: Any = None, *, session_id: str = "",
        file_path: str | None = None, timeout: float = 1200.0,
    ) -> dict[str, Any]:
        """Send one step's input; reports ``input_required`` with the step that waits, or completion."""
        base_url = self._base_url(workflow_id)
        session = self._session(workflow_id, session_id)
        payload: dict[str, Any] = {"sessionId": session, "stepId": step_id, "input": input}
        if file_path:
            payload["file_path"] = file_path
        events = _sse_payloads(_http(f"{base_url}/api/run-step", payload, timeout))
        awaiting = next((event.get("value") for event in events if event.get("name") == "input_required"), None)
        final = next((event["result"] for event in reversed(events) if isinstance(event.get("result"), dict)), None)
        return {
            "workflow_id": workflow_id, "session_id": session, "step_id": step_id,
            "status": "input_required" if awaiting else "completed",
            "awaiting": awaiting, "final": final, "events": events,
        }

    def reset_session(self, workflow_id: str, session_id: str = "") -> dict[str, Any]:
        """Cancel the session's paused run; the next ``run_step`` starts a fresh one."""
        base_url = self._base_url(workflow_id)
        session = self._session(workflow_id, session_id)
        reset = json.loads(_http(f"{base_url}/api/reset-session", {"sessionId": session}, 30.0))
        return {"workflow_id": workflow_id, "session_id": session, "reset": reset}

    # -- results ----------------------------------------------------------------------------
    def _runs_dir(self, workflow_id: str) -> Path:
        return self.builder.record(workflow_id).storage_dir / "runs"

    def list_runs(self, workflow_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Recent runs journaled by the service, newest first."""
        runs: list[dict[str, Any]] = []
        for run_file in self._runs_dir(workflow_id).glob("*/run.json"):
            try:
                data = json.loads(run_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            runs.append({
                key: data.get(key)
                for key in ("run_id", "session_id", "workflow_version", "status", "created_at", "updated_at", "error")
            })
        runs.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return runs[: max(1, int(limit))]

    def get_run(self, workflow_id: str, run_id: str = "latest") -> dict[str, Any]:
        """One run's journal: per-node status, cards and the question it waits on."""
        runs_dir = self._runs_dir(workflow_id)
        if run_id == "latest":
            latest = runs_dir / "latest.json"
            if not latest.is_file():
                raise FileNotFoundError(f"workflow {workflow_id} has no runs yet")
            run_id = json.loads(latest.read_text(encoding="utf-8"))["run_id"]
        if not _SAFE_RUN_ID.match(run_id):
            raise ValueError("run_id must be a plain run identifier")
        path = runs_dir / run_id / "run.json"
        if not path.is_file():
            raise FileNotFoundError(f"workflow {workflow_id} has no run {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def evidence(self, workflow_id: str, run_id: str = "latest") -> dict[str, Any]:
        """What to hand to ``begin_build(runtime_evidence=...)``: the run journal and the service log tail."""
        record = self.builder.record(workflow_id)
        try:
            run: Any = self.get_run(workflow_id, run_id)
        except FileNotFoundError:
            run = None
        return {"run": run, "service_log_tail": _tail(self.log_path(record)), "errors": dict(record.errors)}

    # -- build integration ------------------------------------------------------------------
    def smoke_test(self, record: WorkflowRecord, main_path: Path) -> tuple[bool, str]:
        """Start the entrypoint, wait until it answers, stop it and audit its log for errors."""
        log_path = record.main_log_path
        with self._lock:
            _terminate(record.process)
            record.process = None
            process = self._spawn(record, Path(main_path), log_path)
            failure = ""
            try:
                self._wait_ready(record, process, log_path)
            except RuntimeError as exc:
                failure = str(exc)
            finally:
                _terminate(process)
                handle = self._logs.pop(record.workflow_id, None)
                if handle is not None:
                    handle.close()
        text = _tail(log_path, 20000)
        ok, violations = OutputAuditor().audit_log_file(str(log_path))
        if ok and not failure:
            return True, text
        report = "\n".join(f"{item.rule}: {item.detail}" for item in violations)
        return False, "\n\n".join(part for part in (failure, f"run log reported errors:\n{report}" if report else "", text[-6000:]) if part)


__all__ = ["ServiceManager"]
