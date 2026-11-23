# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-19: read-only FlowX workspace inspection shared by the Hermes agent plugin and dashboard API.
# Modified 2026-11-23: FlowX calls no LLM, so the viewer no longer reports provider or model settings.

"""Read-only inspection of a FlowX workspace for the Hermes plugin.

The module is stdlib-only so the dashboard backend can run without importing the
FlowX SDK into Hermes's Python process. It understands the on-disk layout written
by ``flowx_core``:

``<workspace>/workflows/<workflow_id>/``
    ``agent.json``, ``manifest.json``, ``plan/workflow.json``,
    ``plan/requirement_analysis.md``, ``nodes/<node_id>/<version>/...``,
    ``service/service_<id>/...``, ``logs/main_run.log``, ``runs/<run_id>/run.json``,
    and optional media under ``runs/<run_id>/media/...``.

Nothing here writes to a workflow except ``canvas_layout.json`` for the viewer.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MAX_TEXT_BYTES = 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_MEDIA_SLICE = 8 * 1024 * 1024
MAX_RUNS = 50
MAX_FILE_ENTRIES = 500
MAX_WORKFLOWS = 200
MAX_POSITION_NODES = 500
MAX_REDACT_DEPTH = 24
MAX_REDACT_ITEMS = 2000

WORKFLOW_SEGMENT_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_STATUS_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SECRET_PATH_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|"
    r"private[_-]?key|(?:^|[._-])id[_-](?:rsa|dsa|ecdsa|ed25519)(?:[._-]|$)|\.(?:key|pem|p12|pfx)$)",
    re.IGNORECASE,
)
SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key)",
    re.IGNORECASE,
)
TOKEN_TEXT_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,})\b"
)
BEARER_TEXT_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"',}]+")
ASSIGN_TEXT_RE = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?key|secret|token|password|passwd|credential)\s*[:=]\s*)"
    r"([\"']?)([^\s\"',}]+)"
)
URL_CREDENTIALS_RE = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)

TEXT_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".py",
    ".sql",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


class FlowXError(Exception):
    """A bounded, user-safe error for the dashboard API and agent tools."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Settings:
    """Resolved workspace settings for one request."""

    workspace: Path
    workflows_dir: Path
    single_workflow: bool
    single_id: str
    python: str
    source: str

    @property
    def workspace_exists(self) -> bool:
        return self.workspace.is_dir()


def _utc_iso(timestamp: float | None = None) -> str:
    value = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp is not None else datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds")


def _read_hermes_plugin_settings() -> Mapping[str, Any]:
    """Read ``plugins.entries.flowx.settings`` when running inside Hermes.

    Imports are attempted lazily and every failure falls back to environment
    variables, which keeps the module usable in tests and standalone previews.
    """

    try:
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        config = load_config_readonly() or {}
    except Exception:
        try:
            from hermes_cli.config import load_config  # type: ignore[import-not-found]

            config = load_config() or {}
        except Exception:
            return {}
    try:
        entries = config.get("plugins", {}).get("entries", {})
        settings = entries.get("flowx", {}).get("settings", {})
        return settings if isinstance(settings, Mapping) else {}
    except Exception:
        return {}


def load_settings() -> Settings:
    """Resolve workspace, interpreter, and display settings for this call."""

    configured = _read_hermes_plugin_settings()
    workspace_value = str(
        configured.get("workspace")
        or os.environ.get("FLOWX_WORKSPACE")
        or os.environ.get("FLOWX_DEFAULT_WORKSPACE")
        or ""
    ).strip()
    source = "settings" if configured.get("workspace") else (
        "environment" if (os.environ.get("FLOWX_WORKSPACE") or os.environ.get("FLOWX_DEFAULT_WORKSPACE")) else "default"
    )
    if not workspace_value:
        workspace_value = str(Path.home() / ".flowx_workspaces")
        source = "default"
    try:
        workspace = Path(workspace_value).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise FlowXError("workspace-invalid", "The configured FlowX workspace path is invalid.") from exc
    workflows = workspace / "workflows"
    single = False
    single_id = ""
    if not workflows.is_dir() and (workspace / "plan" / "workflow.json").is_file():
        workflows = workspace.parent
        single = True
        single_id = workspace.name
    python_value = str(configured.get("python") or os.environ.get("FLOWX_PYTHON") or "").strip()
    return Settings(
        workspace=workspace,
        workflows_dir=workflows,
        single_workflow=single,
        single_id=single_id,
        python=python_value,
        source=source,
    )


def config_info() -> dict[str, Any]:
    """Small settings payload shown by the viewer header."""

    settings = load_settings()
    return {
        "ok": True,
        "workspace": str(settings.workspace),
        "workspace_exists": settings.workspace_exists,
        "workflows_dir": str(settings.workflows_dir),
        "single_workflow": settings.single_workflow,
        "python": settings.python,
        "settings_source": settings.source,
    }


def _safe_segment(value: Any, *, code: str = "invalid-workflow") -> str:
    if not isinstance(value, str):
        raise FlowXError(code, "A workflow identifier is required.")
    text = value.strip()
    if not text or text in {".", ".."} or not WORKFLOW_SEGMENT_RE.match(text) or SECRET_PATH_RE.search(text):
        raise FlowXError(code, "The workflow identifier is invalid.")
    return text


def _safe_relative(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise FlowXError("invalid-path", "A relative path is required.")
    if value == "" and allow_empty:
        return ""
    text = value.strip()
    if (
        not text
        or len(text) > 4096
        or text.startswith(("/", "~"))
        or "\\" in text
        or "\x00" in text
        or re.match(r"^[A-Za-z]:", text)
    ):
        raise FlowXError("invalid-path", "The path is invalid.")
    parts = text.split("/")
    for part in parts:
        if part in {"", ".", ".."} or part.startswith(".") or SECRET_PATH_RE.search(part):
            raise FlowXError("invalid-path", "The path is unavailable or unsafe.")
    return text


def _workflow_dir(settings: Settings, workflow_id: Any) -> Path:
    resolved_id = _safe_segment(workflow_id)
    if settings.single_workflow:
        if resolved_id != settings.single_id:
            raise FlowXError("unknown-workflow", "Unknown workflow.", 404)
        return settings.workspace
    if settings.workflows_dir.is_symlink():
        raise FlowXError("workspace-unavailable", "The FlowX workspace is unavailable.", 404)
    try:
        root = settings.workflows_dir.resolve(strict=True)
    except OSError as exc:
        raise FlowXError("workspace-unavailable", "The FlowX workspace is unavailable.", 404) from exc
    candidate = root / resolved_id
    try:
        real = candidate.resolve(strict=True)
    except OSError as exc:
        raise FlowXError("unknown-workflow", "Unknown workflow.", 404) from exc
    if candidate.is_symlink() or real.parent != root or real == root or not real.is_dir():
        raise FlowXError("unknown-workflow", "Unknown workflow.", 404)
    return real


def _relative_inside(root: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        real = candidate.resolve(strict=True)
    except OSError:
        return None
    try:
        rel = real.relative_to(root.resolve())
    except ValueError:
        return None
    text = rel.as_posix()
    try:
        return _safe_relative(text) if text else None
    except FlowXError:
        return None


def _resolve_file(root: Path, relative: Any) -> Path:
    rel = _safe_relative(relative)
    candidate = root / rel
    try:
        real = candidate.resolve(strict=True)
    except OSError as exc:
        raise FlowXError("file-not-found", "The file is unavailable.", 404) from exc
    if candidate.is_symlink() or not real.is_relative_to(root) or real == root or not real.is_file():
        raise FlowXError("file-not-found", "The file is unavailable.", 404)
    return real


def _resolve_dir(root: Path, relative: Any) -> Path:
    rel = _safe_relative(relative, allow_empty=True)
    candidate = root / rel if rel else root
    try:
        real = candidate.resolve(strict=True)
    except OSError as exc:
        raise FlowXError("folder-not-found", "The folder is unavailable.", 404) from exc
    if candidate.is_symlink() or not real.is_relative_to(root) or not real.is_dir():
        raise FlowXError("folder-not-found", "The folder is unavailable.", 404)
    return real


def _open_readonly(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    return os.open(path, flags)


def _read_bytes(path: Path, limit: int) -> bytes:
    descriptor = _open_readonly(path)
    try:
        stat = os.fstat(descriptor)
        if not os.path.isfile(path) or stat.st_size > limit:
            raise FlowXError("file-too-large", "The file exceeds the viewer limit.", 413)
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(65536, limit - total + 1))
            if not block:
                break
            total += len(block)
            if total > limit:
                raise FlowXError("file-too-large", "The file exceeds the viewer limit.", 413)
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_json(path: Path, *, limit: int = MAX_JSON_BYTES) -> Any:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = _read_bytes(path, limit)
        return json.loads(raw.decode("utf-8"))
    except (FlowXError, OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _redact_text(value: str) -> str:
    text = TOKEN_TEXT_RE.sub("[redacted]", value)
    text = BEARER_TEXT_RE.sub(r"\1[redacted]", text)
    text = ASSIGN_TEXT_RE.sub(r"\1\2[redacted]", text)
    text = URL_CREDENTIALS_RE.sub(r"\1[redacted]@", text)
    return text


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_REDACT_DEPTH:
        return "[truncated]"
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_REDACT_ITEMS:
                clean["__truncated__"] = True
                break
            name = _redact_text(str(key))
            clean[name] = "[redacted]" if SECRET_KEY_RE.search(str(key)) else _sanitize(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:MAX_REDACT_ITEMS]]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _redact_text(str(value))


def _safe_status(value: Any, default: str = "unknown") -> str:
    if isinstance(value, str) and SAFE_STATUS_RE.match(value):
        return value
    return default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _graph(wf_dir: Path) -> dict[str, Any]:
    plan_path = wf_dir / "plan" / "workflow.json"
    raw = _read_json(plan_path)
    if raw is None:
        return {"available": False, "nodes": [], "edges": [], "missing_dependencies": [], "roots": [], "terminal": []}
    items = raw.get("nodes") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list):
        items = []
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in items[:1000]:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("id") or item.get("step_id")
        if not isinstance(name, str) or not name.strip():
            continue
        node_id = name.strip()
        if node_id in by_id:
            continue
        dependencies: list[str] = []
        for dependency in item.get("depends", []) or []:
            if isinstance(dependency, str) and dependency.strip():
                dependencies.append(dependency.strip())
            elif isinstance(dependency, Mapping):
                dep_name = dependency.get("name") or dependency.get("id")
                if isinstance(dep_name, str) and dep_name.strip():
                    dependencies.append(dep_name.strip())
        meta_node_kind = item.get("meta_node_kind") or item.get("metaNodeKind") or ""
        ext_data = item.get("ext_data") if isinstance(item.get("ext_data"), Mapping) else item.get("extData")
        node = {
            "id": node_id,
            "name": node_id,
            "type": _redact_text(str(item.get("type") or "")),
            "description": _redact_text(str(item.get("desc") or item.get("description") or "")),
            "meta_node_kind": _redact_text(str(meta_node_kind or "")),
            "ext_data": _sanitize(ext_data if isinstance(ext_data, Mapping) else {}),
            "inputs_format": _sanitize(item.get("inputs_format") if isinstance(item.get("inputs_format"), Mapping) else {}),
            "loop": item.get("loop") if isinstance(item.get("loop"), int) and not isinstance(item.get("loop"), bool) else None,
            "enabled": item.get("enable", True) is not False,
            "dependencies": dependencies,
        }
        nodes.append(node)
        by_id[node_id] = node
    edges: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for node in nodes:
        for dependency in node["dependencies"]:
            if dependency in by_id:
                edges.append({"from": dependency, "to": node["id"]})
            else:
                missing.append({"from": dependency, "to": node["id"]})
    parents = {edge["from"] for edge in edges}
    children = {edge["to"] for edge in edges}
    roots = [node["id"] for node in nodes if node["id"] not in children]
    terminal = [node["id"] for node in nodes if node["id"] not in parents]
    return {
        "available": True,
        "path": "plan/workflow.json" if plan_path.is_file() else None,
        "nodes": nodes,
        "edges": edges,
        "missing_dependencies": missing,
        "roots": roots,
        "terminal": terminal,
    }


def _node_records(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        return {}
    records: Any = manifest.get("nodes")
    workflows = manifest.get("workflows")
    if isinstance(workflows, list) and workflows:
        latest = workflows[-1]
        if isinstance(latest, Mapping) and isinstance(latest.get("nodes"), Mapping):
            records = latest.get("nodes")
    return records if isinstance(records, Mapping) else {}


def _normalize_node_records(wf_dir: Path, manifest: Any) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for name, record in list(_node_records(manifest).items())[:1000]:
        if not isinstance(name, str) or not isinstance(record, Mapping):
            continue
        artifacts_raw = record.get("artifacts")
        artifacts: dict[str, str] = {}
        if isinstance(artifacts_raw, Mapping):
            for key, value in list(artifacts_raw.items())[:100]:
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                relative = _relative_inside(wf_dir, value)
                if relative:
                    artifacts[key] = relative
        normalized[name] = {
            "node_id": str(record.get("node_id") or ""),
            "version": _integer(record.get("version"), 0),
            "artifacts": artifacts,
        }
    return normalized


def _latest_run_summary(wf_dir: Path) -> dict[str, Any] | None:
    latest = _read_json(wf_dir / "runs" / "latest.json", limit=MAX_TEXT_BYTES)
    if not isinstance(latest, Mapping):
        return None
    run_id = latest.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return None
    return {
        "run_id": run_id,
        "status": _safe_status(latest.get("status")),
        "updated_at": latest.get("updated_at") if isinstance(latest.get("updated_at"), str) else None,
    }


def _workflow_summary(wf_dir: Path) -> dict[str, Any]:
    workflow_id = wf_dir.name
    agent = _read_json(wf_dir / "agent.json")
    manifest = _read_json(wf_dir / "manifest.json")
    graph = _graph(wf_dir)
    node_records = _normalize_node_records(wf_dir, manifest)
    errors = []
    if isinstance(agent, Mapping) and isinstance(agent.get("errors"), Mapping):
        errors = sorted(_redact_text(str(key)) for key in agent["errors"])[:100]
    latest_run = _latest_run_summary(wf_dir)
    main_path = _relative_inside(wf_dir, agent.get("main_entrypoint_path")) if isinstance(agent, Mapping) else None
    version = _integer(manifest.get("workflow_version"), 0) if isinstance(manifest, Mapping) else 0
    if not version and isinstance(agent, Mapping):
        version = _integer(agent.get("main_workflow_version"), 0)
    mtimes = [
        path.stat().st_mtime
        for path in (wf_dir / "agent.json", wf_dir / "manifest.json", wf_dir / "plan" / "workflow.json")
        if path.is_file()
    ]
    updated_at = _utc_iso(max(mtimes)) if mtimes else None
    if latest_run and latest_run.get("updated_at"):
        updated_at = latest_run["updated_at"]
    return {
        "workflow_id": workflow_id,
        "workflow_version": version,
        "main_entrypoint_id": str(agent.get("main_entrypoint_id") or "") if isinstance(agent, Mapping) else "",
        "main_entrypoint_version": _integer(agent.get("main_entrypoint_version"), 0) if isinstance(agent, Mapping) else 0,
        "main_entrypoint_path": main_path,
        "has_graph": bool(graph.get("available")),
        "has_main": bool(main_path),
        "node_count": len([node for node in graph.get("nodes", []) if node.get("enabled")]),
        "built_node_count": len(node_records),
        "error_count": len(errors),
        "errors": errors,
        "updated_at": updated_at,
        "latest_run": latest_run,
    }


def list_workflows() -> list[dict[str, Any]]:
    """Summaries for every workflow in the configured workspace."""

    settings = load_settings()
    if settings.single_workflow:
        candidates = [settings.workspace]
    else:
        if not settings.workflows_dir.is_dir() or settings.workflows_dir.is_symlink():
            return []
        try:
            candidates = [
                child
                for child in sorted(settings.workflows_dir.iterdir(), key=lambda path: path.name.lower())
                if child.is_dir() and not child.is_symlink() and WORKFLOW_SEGMENT_RE.match(child.name)
            ]
        except OSError as exc:
            raise FlowXError("workspace-unavailable", "The FlowX workspace is unavailable.", 404) from exc
    summaries = []
    for candidate in candidates[:MAX_WORKFLOWS]:
        if not ((candidate / "agent.json").is_file() or (candidate / "plan" / "workflow.json").is_file()):
            continue
        try:
            summaries.append(_workflow_summary(candidate))
        except OSError:
            continue
    summaries.sort(key=lambda item: (item.get("updated_at") or "", item["workflow_id"]), reverse=True)
    return summaries


def get_workflow(workflow_id: Any) -> dict[str, Any]:
    """Full read-only view of one workflow: graph, node artifacts, and main path."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    try:
        summary = _workflow_summary(wf_dir)
    except OSError as exc:
        raise FlowXError("workflow-unavailable", "The workflow is unavailable.", 404) from exc
    graph = _graph(wf_dir)
    manifest = _read_json(wf_dir / "manifest.json")
    requirement_path = wf_dir / "plan" / "requirement_analysis.md"
    return {
        **summary,
        "graph": graph,
        "nodes": _normalize_node_records(wf_dir, manifest),
        "requirement_path": "plan/requirement_analysis.md" if requirement_path.is_file() else None,
        "graph_path": "plan/workflow.json" if (wf_dir / "plan" / "workflow.json").is_file() else None,
        "log_path": "logs/main_run.log" if (wf_dir / "logs" / "main_run.log").is_file() else None,
    }


def read_requirement(workflow_id: Any) -> dict[str, Any]:
    """The workflow's requirement analysis as UTF-8 text."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    path = _resolve_file(wf_dir, "plan/requirement_analysis.md")
    content = _read_bytes(path, MAX_TEXT_BYTES).decode("utf-8", errors="replace")
    return {"path": "plan/requirement_analysis.md", "content": _redact_text(content), "bytes": path.stat().st_size}


def _sanitize_run(raw: Any, expected_run_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("run_id") != expected_run_id:
        raise FlowXError("run-invalid", "The workflow run record is invalid or unsafe.", 404)
    nodes: dict[str, Any] = {}
    source_nodes = raw.get("nodes")
    if isinstance(source_nodes, Mapping):
        for index, (step_id, entry) in enumerate(source_nodes.items()):
            if index >= 500 or not isinstance(step_id, str) or not isinstance(entry, Mapping):
                continue
            nodes[step_id] = {
                "status": _safe_status(entry.get("status"), "completed"),
                "title": str(entry.get("title") or step_id),
                "card": _sanitize(entry.get("card") if isinstance(entry.get("card"), Mapping) else {}),
                "is_final": bool(entry.get("is_final")),
                "updated_at": entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else None,
            }
    return {
        "run_id": expected_run_id,
        "workflow_version": _integer(raw.get("workflow_version"), 0),
        "status": _safe_status(raw.get("status")),
        "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        "awaiting": _sanitize(raw.get("awaiting")) if isinstance(raw.get("awaiting"), Mapping) else None,
        "has_error": bool(raw.get("error")),
        "nodes": nodes,
    }


def list_runs(workflow_id: Any) -> dict[str, Any]:
    """Recent run journals for one workflow, newest first."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    runs_dir = wf_dir / "runs"
    summary = _latest_run_summary(wf_dir)
    latest_id = summary["run_id"] if summary else None
    if not runs_dir.is_dir() or runs_dir.is_symlink():
        return {"latestRunId": latest_id, "runs": []}
    runs: list[dict[str, Any]] = []
    try:
        entries = sorted(runs_dir.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise FlowXError("runs-unavailable", "The workflow runs directory is unavailable.", 404) from exc
    for entry in entries[:1000]:
        if not entry.is_dir() or entry.is_symlink() or not RUN_ID_RE.match(entry.name):
            continue
        raw = _read_json(entry / "run.json", limit=MAX_JSON_BYTES)
        if raw is None:
            continue
        try:
            run = _sanitize_run(raw, entry.name)
        except FlowXError:
            continue
        runs.append(
            {
                "run_id": run["run_id"],
                "workflow_version": run["workflow_version"],
                "status": run["status"],
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
                "node_count": len(run["nodes"]),
                "has_error": run["has_error"],
            }
        )
    runs.sort(key=lambda item: (item.get("updated_at") or item.get("created_at") or "", item["run_id"]), reverse=True)
    bounded = runs[:MAX_RUNS]
    if latest_id and not any(item["run_id"] == latest_id for item in bounded):
        latest_id = bounded[0]["run_id"] if bounded else None
    return {"latestRunId": latest_id or (bounded[0]["run_id"] if bounded else None), "runs": bounded}


def get_run(workflow_id: Any, run_id: Any) -> dict[str, Any]:
    """One sanitized run journal."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise FlowXError("invalid-run", "The run identifier is invalid.")
    raw = _read_json(wf_dir / "runs" / run_id / "run.json", limit=MAX_JSON_BYTES)
    if raw is None:
        raise FlowXError("run-not-found", "The workflow run is unavailable.", 404)
    return _sanitize_run(raw, run_id)


def _file_kind(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix in MEDIA_TYPES:
        return "media", MEDIA_TYPES[suffix]
    if suffix in TEXT_EXTENSIONS:
        return "text", "text/plain; charset=utf-8"
    return "other", "application/octet-stream"


def list_files(workflow_id: Any, relative: Any = "") -> dict[str, Any]:
    """Bounded listing of a workflow folder for the file browser."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    directory = _resolve_dir(wf_dir, relative)
    entries = []
    truncated = False
    try:
        children = sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
    except OSError as exc:
        raise FlowXError("folder-unavailable", "The folder is unavailable.", 404) from exc
    for child in children:
        if len(entries) >= MAX_FILE_ENTRIES:
            truncated = True
            break
        name = child.name
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
            continue
        if name.startswith(".") or SECRET_PATH_RE.search(name) or child.is_symlink():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        rel = child.relative_to(wf_dir).as_posix()
        try:
            _safe_relative(rel)
        except FlowXError:
            continue
        if child.is_dir():
            entries.append({"name": name, "path": rel, "kind": "dir", "size": None, "readable": True})
        elif child.is_file():
            kind, mime = _file_kind(child)
            entries.append(
                {
                    "name": name,
                    "path": rel,
                    "kind": kind,
                    "mime": mime,
                    "size": stat.st_size,
                    "readable": kind in {"text", "media"} and stat.st_size <= (
                        MAX_TEXT_BYTES if kind == "text" else MAX_MEDIA_BYTES
                    ),
                }
            )
    return {"path": _safe_relative(relative, allow_empty=True), "entries": entries, "truncated": truncated}


def read_text(workflow_id: Any, relative: Any) -> dict[str, Any]:
    """Read one UTF-8 text artifact, with structural JSON redaction."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    path = _resolve_file(wf_dir, relative)
    kind, _mime = _file_kind(path)
    if kind != "text" or path.suffix.lower() not in TEXT_EXTENSIONS:
        raise FlowXError("unsupported-file", "This file type is not readable in the viewer.", 415)
    raw = _read_bytes(path, MAX_TEXT_BYTES)
    rel = path.relative_to(wf_dir).as_posix()
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            content = _redact_text(raw.decode("utf-8", errors="replace"))
        else:
            if rel == "agent.json" and isinstance(parsed, Mapping):
                parsed = dict(parsed)
                if isinstance(parsed.get("errors"), Mapping):
                    parsed["errors"] = {str(key): "Details withheld." for key in parsed["errors"]}
                parsed["main_entrypoint_path"] = _relative_inside(wf_dir, parsed.get("main_entrypoint_path"))
            elif rel.startswith("runs/") and rel.endswith("/run.json"):
                run_id = rel.split("/")[1]
                parsed = _sanitize_run(parsed, run_id)
            content = json.dumps(_sanitize(parsed), ensure_ascii=False, indent=2)
    else:
        content = _redact_text(raw.decode("utf-8", errors="replace"))
    return {"path": rel, "content": content, "bytes": len(raw)}


def _valid_positions(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, Mapping) or len(value) > MAX_POSITION_NODES:
        raise FlowXError("invalid-layout", "The canvas layout is invalid.")
    positions: dict[str, dict[str, int]] = {}
    for name, coordinate in value.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or not isinstance(coordinate, Mapping)
        ):
            raise FlowXError("invalid-layout", "The canvas layout is invalid.")
        try:
            x = float(coordinate.get("x"))
            y = float(coordinate.get("y"))
        except (TypeError, ValueError) as exc:
            raise FlowXError("invalid-layout", "The canvas layout is invalid.") from exc
        if not math.isfinite(x) or not math.isfinite(y) or abs(x) > 100000 or abs(y) > 100000:
            raise FlowXError("invalid-layout", "The canvas layout is invalid.")
        positions[name] = {"x": round(x), "y": round(y)}
    return positions


def get_layout(workflow_id: Any) -> dict[str, Any]:
    """Saved graph positions, or an empty map."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    raw = _read_json(wf_dir / "canvas_layout.json", limit=MAX_TEXT_BYTES)
    if not isinstance(raw, Mapping) or not isinstance(raw.get("positions"), Mapping):
        return {"positions": {}}
    try:
        return {"positions": _valid_positions(raw["positions"])}
    except FlowXError:
        return {"positions": {}}


def save_layout(workflow_id: Any, positions: Any) -> dict[str, Any]:
    """Atomically persist viewer positions to ``canvas_layout.json``."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    clean = _valid_positions(positions)
    target = wf_dir / "canvas_layout.json"
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise FlowXError("layout-unavailable", "The canvas layout path is unsafe.", 409)
    temporary = wf_dir / f".canvas_layout-{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        payload = json.dumps({"schema_version": 1, "positions": clean}, ensure_ascii=False, indent=2).encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, target)
    except OSError as exc:
        raise FlowXError("layout-unavailable", "Unable to save the canvas layout.", 409) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return {"positions": clean}


def _parse_byte_range(header: str | None, total: int) -> Any:
    if not header:
        return None
    match = re.match(r"^bytes=(\d*)-(\d*)$", header.strip())
    if not match or (match.group(1) == "" and match.group(2) == "") or total == 0:
        return "invalid"
    if match.group(1) == "":
        suffix = int(match.group(2))
        if suffix <= 0:
            return "invalid"
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(match.group(1))
        end = total - 1 if match.group(2) == "" else int(match.group(2))
        if start > end or start >= total:
            return "invalid"
        end = min(end, total - 1)
    return {"start": start, "end": min(end, start + MAX_MEDIA_SLICE - 1)}


def read_media(
    workflow_id: Any,
    relative: Any,
    *,
    method: str = "GET",
    range_header: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Read one media file with bounded range support."""

    settings = load_settings()
    wf_dir = _workflow_dir(settings, workflow_id)
    path = _resolve_file(wf_dir, relative)
    mime = MEDIA_TYPES.get(path.suffix.lower())
    if not mime:
        raise FlowXError("unsupported-media", "Unsupported media type.", 415)
    stat = path.stat()
    if stat.st_size > MAX_MEDIA_BYTES:
        raise FlowXError("media-too-large", "The media file exceeds the viewer limit.", 413)
    headers = {
        "content-type": mime,
        "accept-ranges": "bytes",
        "cache-control": "private, no-store",
        "x-content-type-options": "nosniff",
    }
    parsed = _parse_byte_range(range_header, stat.st_size)
    if parsed == "invalid":
        return 416, {**headers, "content-range": f"bytes */{stat.st_size}"}, b""
    start = parsed["start"] if parsed else 0
    end = parsed["end"] if parsed else min(stat.st_size - 1, MAX_MEDIA_SLICE - 1)
    length = 0 if stat.st_size == 0 else end - start + 1
    status = 206 if parsed or stat.st_size > length else 200
    response_headers = {
        **headers,
        "content-length": str(length),
        **({"content-range": f"bytes {start}-{end}/{stat.st_size}"} if status == 206 else {}),
    }
    if method.upper() == "HEAD":
        return status, response_headers, b""
    descriptor = _open_readonly(path)
    try:
        if os.fstat(descriptor).st_size != stat.st_size:
            raise FlowXError("media-unavailable", "The media file changed while reading.", 409)
        body = os.pread(descriptor, length, start) if hasattr(os, "pread") else os.read(descriptor, length)
        if len(body) != length:
            raise FlowXError("media-unavailable", "The media file changed while reading.", 409)
    finally:
        os.close(descriptor)
    return status, response_headers, body


__all__ = [
    "FlowXError",
    "Settings",
    "config_info",
    "get_layout",
    "get_run",
    "get_workflow",
    "list_files",
    "list_runs",
    "list_workflows",
    "load_settings",
    "read_media",
    "read_requirement",
    "read_text",
    "save_layout",
]
