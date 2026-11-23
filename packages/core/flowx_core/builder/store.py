# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""On-disk workflow layout: plan files, versioned node bundles and the manifest.

Layout under ``<workspace>/workflows/<workflow_id>/``::

    plan/{workflow.json, requirement_analysis.md, brief.md}
    nodes/<node_id>/<version>/...        every published node version
    workflows/<version>/...              self-contained workflow snapshots
    manifest.json                        node registry and workflow history
    agent.json                           ids, entrypoint and unresolved errors
    service/service_<main_entrypoint_id> importable package with ``main.py``
    build/                               journal, ledger and staging (ephemeral)
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from flowx_core.tools.artifact_tools import fingerprint_json, write_json_atomic

MANIFEST_SCHEMA = 1


def artifact_targets(directory: Path, name: str) -> dict[str, Path]:
    """Where one node's artifacts live inside a version directory."""
    return {
        "source": directory / f"{name}.py",
        "markdown": directory / "node_docs" / f"{name}.md",
        "test": directory / "tests" / f"test_{name}.py",
        "log": directory / "logs" / f"{name}_test.log",
    }


@dataclass
class WorkflowRecord:
    """One workflow's ids, entrypoint state and unresolved errors (``agent.json``).

    Errors are keyed ``graph``, ``main`` or ``nodes/<step id>`` and cleared when that part
    next succeeds.
    """

    workflow_id: str
    storage_dir: Path
    main_entrypoint_id: str
    main_entrypoint_version: int = 0
    main_workflow_version: int = 0
    main_entrypoint_path: Path | None = None
    fastapi_host: str = "0.0.0.0"
    fastapi_port: int = 8000
    errors: dict[str, str] = field(default_factory=dict)
    process: Any = field(default=None, repr=False)

    @property
    def graph_path(self) -> Path:
        return self.storage_dir / "plan" / "workflow.json"

    @property
    def requirement_path(self) -> Path:
        return self.storage_dir / "plan" / "requirement_analysis.md"

    @property
    def brief_path(self) -> Path:
        return self.storage_dir / "plan" / "brief.md"

    @property
    def service_dir(self) -> Path:
        # An importable package name: the generated main imports its nodes from it.
        return self.storage_dir / "service" / f"service_{self.main_entrypoint_id}"

    @property
    def main_log_path(self) -> Path:
        return self.storage_dir / "logs" / "main_run.log"

    @property
    def base_url(self) -> str:
        """Where a client reaches the running service; bind addresses map to loopback."""
        host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(self.fastapi_host, self.fastapi_host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.fastapi_port}"

    def clear_errors(self, prefix: str) -> None:
        for key in [key for key in self.errors if key == prefix or key.startswith(f"{prefix}/")]:
            del self.errors[key]

    def save(self) -> None:
        write_json_atomic(self.storage_dir / "agent.json", {
            "workflow_id": self.workflow_id,
            "main_entrypoint_id": self.main_entrypoint_id,
            "main_entrypoint_version": self.main_entrypoint_version,
            "main_workflow_version": self.main_workflow_version,
            "main_entrypoint_path": str(self.main_entrypoint_path) if self.main_entrypoint_path else None,
            "fastapi_host": self.fastapi_host,
            "fastapi_port": self.fastapi_port,
            "errors": self.errors,
        })

    @classmethod
    def load(cls, storage_dir: Path) -> "WorkflowRecord":
        data = json.loads((storage_dir / "agent.json").read_text(encoding="utf-8"))
        return cls(
            workflow_id=data["workflow_id"], storage_dir=storage_dir,
            main_entrypoint_id=data["main_entrypoint_id"],
            main_entrypoint_version=data["main_entrypoint_version"],
            main_workflow_version=data["main_workflow_version"],
            main_entrypoint_path=Path(data["main_entrypoint_path"]) if data["main_entrypoint_path"] else None,
            fastapi_host=data.get("fastapi_host", "0.0.0.0"),
            fastapi_port=int(data.get("fastapi_port", 8000)),
            errors=dict(data.get("errors", {})),
        )


class WorkflowStore:
    """Plan files and the versioned artifact manifest of one workflow."""

    def __init__(self, record: WorkflowRecord) -> None:
        self.record = record
        self.directory = record.storage_dir
        self.manifest_path = self.directory / "manifest.json"
        self._lock = RLock()
        if not self.manifest_path.is_file():
            write_json_atomic(self.manifest_path, {
                "schema_version": MANIFEST_SCHEMA,
                "workflow_id": record.workflow_id,
                "workflow_version": 0,
                "workflows": [],
                "nodes": {},
            })

    @property
    def workflow_id(self) -> str:
        return self.record.workflow_id

    # -- plan ---------------------------------------------------------------------------------
    def read_requirement(self) -> str:
        path = self.record.requirement_path
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def write_requirement(self, text: str) -> None:
        self._write_text(self.record.requirement_path, text)

    def read_graph(self) -> dict[str, Any] | None:
        path = self.record.graph_path
        if not path.is_file():
            return None
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return graph if isinstance(graph, dict) else None

    def write_graph(self, graph: Mapping[str, Any]) -> None:
        write_json_atomic(self.record.graph_path, dict(graph))

    def write_brief(self, text: str) -> None:
        self._write_text(self.record.brief_path, text)

    def read_brief(self) -> str:
        path = self.record.brief_path
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def enabled_nodes(self) -> list[str]:
        graph = self.read_graph() or {}
        nodes = graph.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [
            node["name"] for node in nodes
            if isinstance(node, dict) and node.get("enable", True) and isinstance(node.get("name"), str)
        ]

    # -- manifest -----------------------------------------------------------------------------
    def manifest(self) -> dict[str, Any]:
        with self._lock:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
            raise ValueError("unsupported workflow manifest")
        return manifest

    def _resolve(self, relative: str) -> Path:
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("artifact path escapes the workflow directory")
        return path

    def artifacts_of(self, record: Mapping[str, Any]) -> dict[str, Path]:
        return {key: self._resolve(value) for key, value in record.get("artifacts", {}).items()}

    def latest_node(self, name: str) -> dict[str, Any] | None:
        registry = self.manifest()["nodes"].get(name)
        return dict(registry["versions"][-1]) if registry and registry["versions"] else None

    def workflow_version(self) -> int:
        return int(self.manifest()["workflow_version"])

    def latest_workflow(self) -> dict[str, Any] | None:
        workflows = self.manifest()["workflows"]
        return dict(workflows[-1]) if workflows else None

    def node_ids(self) -> dict[str, str]:
        workflow = self.latest_workflow()
        if workflow is None:
            return {}
        return {name: node["node_id"] for name, node in workflow["nodes"].items()}

    def node_record(self, name: str, files: Mapping[str, Path], *, node_id: str, version: int,
                    fingerprint: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "node_id": node_id, "node_name": name, "version": version,
            "input_fingerprint": fingerprint,
            "artifacts": {
                key: str(path.relative_to(self.directory)) for key, path in files.items() if path.is_file()
            },
            **dict(extra or {}),
        }

    def publish_node(self, name: str, fingerprint: str, files: Mapping[str, Path],
                     extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Record a new immutable node version from artifact files; returns its record."""
        with self._lock:
            manifest = self.manifest()
            registry = manifest["nodes"].setdefault(
                name, {"node_id": uuid.uuid4().hex, "version": 0, "versions": []},
            )
            version = registry["version"] + 1
            node_dir = self.directory / "nodes" / registry["node_id"] / f"{version:08d}"
            node_dir.mkdir(parents=True, exist_ok=False)
            try:
                targets = artifact_targets(node_dir, name)
                for key, source in files.items():
                    if key in targets and Path(source).is_file():
                        targets[key].parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, targets[key])
                record = self.node_record(
                    name, targets, node_id=registry["node_id"], version=version,
                    fingerprint=fingerprint, extra=extra,
                )
                write_json_atomic(node_dir / "version.json", record)
                registry["version"] = version
                registry["versions"].append(record)
                write_json_atomic(self.manifest_path, manifest)
            except BaseException:
                shutil.rmtree(node_dir, ignore_errors=True)
                raise
            return dict(record)

    def workflow_fingerprint(self, graph: Mapping[str, Any], requirement: str, node_versions: Mapping[str, int]) -> str:
        return fingerprint_json({"graph": graph, "requirement": requirement, "nodes": dict(node_versions)})

    def publish_workflow(
        self,
        order: list[str],
        graph: Mapping[str, Any],
        requirement: str,
        changed: list[str],
    ) -> tuple[dict[str, Any], bool]:
        """Append a workflow snapshot pinning each node's latest version.

        Returns ``(record, created)``; an identical snapshot is reused, not duplicated.
        """
        with self._lock:
            manifest = self.manifest()
            latest = {name: manifest["nodes"][name]["versions"][-1] for name in order}
            fingerprint = self.workflow_fingerprint(
                graph, requirement, {name: record["version"] for name, record in latest.items()},
            )
            history = manifest["workflows"]
            if history and history[-1]["input_fingerprint"] == fingerprint:
                return dict(history[-1]), False
            version = manifest["workflow_version"] + 1
            directory = self.directory / "workflows" / f"{version:08d}"
            directory.mkdir(parents=True, exist_ok=False)
            try:
                write_json_atomic(directory / "workflow.json", dict(graph))
                (directory / "requirement_analysis.md").write_text(requirement, encoding="utf-8")
                node_records: dict[str, Any] = {}
                for name, record in latest.items():
                    targets = artifact_targets(directory, name)
                    for key, source in self.artifacts_of(record).items():
                        if key in targets and source.is_file():
                            targets[key].parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, targets[key])
                    node_records[name] = self.node_record(
                        name, targets, node_id=record["node_id"], version=record["version"],
                        fingerprint=record["input_fingerprint"],
                    )
                workflow_record = {
                    "workflow_id": self.workflow_id,
                    "version": version,
                    "input_fingerprint": fingerprint,
                    "workflow_path": str((directory / "workflow.json").relative_to(self.directory)),
                    "requirement_md_path": str((directory / "requirement_analysis.md").relative_to(self.directory)),
                    "nodes": node_records,
                    "changed_nodes": list(changed),
                }
                write_json_atomic(directory / "version.json", workflow_record)
                manifest["workflow_version"] = version
                history.append(workflow_record)
                write_json_atomic(self.manifest_path, manifest)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            return dict(workflow_record), True

    def workflow_paths(self, record: Mapping[str, Any]) -> tuple[Path, Path]:
        return self._resolve(record["workflow_path"]), self._resolve(record["requirement_md_path"])


__all__ = ["MANIFEST_SCHEMA", "WorkflowRecord", "WorkflowStore", "artifact_targets"]
