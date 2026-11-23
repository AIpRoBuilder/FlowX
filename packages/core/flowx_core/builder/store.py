# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: record per-file digests, verify FlowX-owned files, and expose read-only artifact access.
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

from flowx_core.tools.artifact_tools import fingerprint_json, text_digest, write_json_atomic

MANIFEST_SCHEMA = 1
_INTEGRITY_REMEDY = (
    "Do not edit files under <workspace>/workflows/ directly; "
    "use FlowX begin_build or begin_evolve to change a workflow."
)
_DIGEST_NODE_KEYS = ("source", "test", "markdown")


class WorkflowFilesModified(RuntimeError):
    """FlowX-owned workflow files were created, modified, or deleted outside FlowX."""

    def __init__(
        self,
        workflow_id: str,
        modified: list[dict[str, Any]],
        remedy: str = _INTEGRITY_REMEDY,
    ) -> None:
        self.workflow_id = workflow_id
        self.modified = [dict(item) for item in modified]
        self.remedy = remedy
        paths = ", ".join(item["path"] for item in self.modified[:5])
        suffix = f" (+{len(self.modified) - 5} more)" if len(self.modified) > 5 else ""
        super().__init__(
            f"workflow {workflow_id} files were modified outside FlowX: {paths}{suffix}. {remedy}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": "workflow_files_modified_outside_flowx",
            "workflow_id": self.workflow_id,
            "modified": list(self.modified),
            "remedy": self.remedy,
        }


def _file_digest(path: Path) -> str:
    return text_digest(path.read_text(encoding="utf-8"))


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

    Errors are keyed ``graph``, ``main``, ``integrity`` or ``nodes/<step id>`` and cleared when that part
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
    plan_digests: dict[str, str] = field(default_factory=dict)
    service_digests: dict[str, str] = field(default_factory=dict)
    digests_tracked: bool = True
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
            "plan_digests": dict(self.plan_digests),
            "service_digests": dict(self.service_digests),
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
            plan_digests=dict(data.get("plan_digests", {})),
            service_digests=dict(data.get("service_digests", {})),
            digests_tracked="plan_digests" in data,
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
    def _plan_entries(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("requirement", self.record.requirement_path),
            ("graph", self.record.graph_path),
            ("brief", self.record.brief_path),
        )

    def _check_digest(self, path: Path, expected: str | None, *, allow_unexpected: bool = False) -> dict[str, Any] | None:
        relative = str(path.relative_to(self.directory))
        if expected is None:
            if not allow_unexpected and path.is_file():
                return {"path": relative, "status": "unexpected", "expected": None, "actual": _file_digest(path)}
            return None
        if not path.is_file():
            return {"path": relative, "status": "missing", "expected": expected, "actual": None}
        actual = _file_digest(path)
        if actual != expected:
            return {"path": relative, "status": "modified", "expected": expected, "actual": actual}
        return None

    def _verify_plan_file(self, key: str, path: Path) -> None:
        if not self.record.digests_tracked:
            return
        mismatch = self._check_digest(path, self.record.plan_digests.get(key))
        if mismatch is not None:
            raise WorkflowFilesModified(self.workflow_id, [mismatch])

    def read_requirement(self, *, verify: bool = True) -> str:
        path = self.record.requirement_path
        if verify:
            self._verify_plan_file("requirement", path)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def write_requirement(self, text: str) -> None:
        self._write_text(self.record.requirement_path, text)
        self.record.plan_digests["requirement"] = text_digest(text)
        self.record.digests_tracked = True
        self.record.save()

    def read_graph(self, *, verify: bool = True) -> dict[str, Any] | None:
        path = self.record.graph_path
        if verify:
            self._verify_plan_file("graph", path)
        if not path.is_file():
            return None
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return graph if isinstance(graph, dict) else None

    def write_graph(self, graph: Mapping[str, Any]) -> None:
        write_json_atomic(self.record.graph_path, dict(graph))
        self.record.plan_digests["graph"] = _file_digest(self.record.graph_path)
        self.record.digests_tracked = True
        self.record.save()

    def write_brief(self, text: str) -> None:
        self._write_text(self.record.brief_path, text)
        self.record.plan_digests["brief"] = text_digest(text)
        self.record.digests_tracked = True
        self.record.save()

    def read_brief(self, *, verify: bool = True) -> str:
        path = self.record.brief_path
        if verify:
            self._verify_plan_file("brief", path)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def plan_digest(self, *, graph: Mapping[str, Any] | None = None, requirement: str | None = None) -> str:
        """Digest of the current (or supplied) graph and requirement pair."""
        resolved_graph = self.read_graph() if graph is None else dict(graph)
        resolved_requirement = self.read_requirement() if requirement is None else requirement
        return fingerprint_json({"graph": resolved_graph, "requirement": resolved_requirement})

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

    def node_version(self, name: str, version: int) -> dict[str, Any] | None:
        registry = self.manifest()["nodes"].get(name)
        if not registry:
            return None
        for entry in registry["versions"]:
            if int(entry.get("version", 0)) == int(version):
                return dict(entry)
        return None

    def version_by_digest(self, name: str, source_digest: str) -> dict[str, Any] | None:
        """Find the published node version matching ``source_digest`` (newest first)."""
        registry = self.manifest()["nodes"].get(name)
        if not registry:
            return None
        for entry in reversed(registry["versions"]):
            recorded = entry.get("source_digest") or entry.get("digests", {}).get("source")
            if recorded == source_digest:
                return dict(entry)
        return None

    def workflow_version(self) -> int:
        return int(self.manifest()["workflow_version"])

    def latest_workflow(self) -> dict[str, Any] | None:
        workflows = self.manifest()["workflows"]
        return dict(workflows[-1]) if workflows else None

    def workflow_snapshot(self, version: int) -> dict[str, Any] | None:
        for entry in self.manifest()["workflows"]:
            if int(entry.get("version", 0)) == int(version):
                return dict(entry)
        return None

    def node_ids(self) -> dict[str, str]:
        workflow = self.latest_workflow()
        if workflow is None:
            return {}
        return {name: node["node_id"] for name, node in workflow["nodes"].items()}

    @staticmethod
    def file_digests(files: Mapping[str, Path]) -> dict[str, str]:
        return {
            key: _file_digest(path)
            for key, path in files.items()
            if key in _DIGEST_NODE_KEYS and path.is_file()
        }

    def node_record(self, name: str, files: Mapping[str, Path], *, node_id: str, version: int,
                    fingerprint: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "node_id": node_id, "node_name": name, "version": version,
            "input_fingerprint": fingerprint,
            "artifacts": {
                key: str(path.relative_to(self.directory)) for key, path in files.items() if path.is_file()
            },
            "digests": self.file_digests(files),
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
                    "plan_digest": self.plan_digest(graph=graph, requirement=requirement),
                    "workflow_path": str((directory / "workflow.json").relative_to(self.directory)),
                    "requirement_md_path": str((directory / "requirement_analysis.md").relative_to(self.directory)),
                    "digests": {
                        "workflow": _file_digest(directory / "workflow.json"),
                        "requirement": _file_digest(directory / "requirement_analysis.md"),
                    },
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

    # -- integrity & read-only content access -------------------------------------------------
    def record_service_digests(self, package_dir: Path) -> dict[str, str]:
        """Record digests of FlowX-rendered service files (never touches ``.env`` or logs)."""
        digests: dict[str, str] = {}
        if package_dir.is_dir():
            for path in sorted(package_dir.iterdir()):
                if path.is_file() and (path.suffix == ".py" or path.name == "workflow.json"):
                    digests[path.name] = _file_digest(path)
        self.record.service_digests = digests
        self.record.save()
        return dict(digests)

    def _node_record_mismatches(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        artifacts = self.artifacts_of(record)
        digests = record.get("digests")
        expected_map = dict(digests) if isinstance(digests, Mapping) else {}
        if "source" not in expected_map and record.get("source_digest"):
            expected_map["source"] = str(record["source_digest"])
        for key, expected in expected_map.items():
            path = artifacts.get(key)
            if path is None:
                continue
            mismatch = self._check_digest(path, str(expected))
            if mismatch is not None:
                mismatches.append(mismatch)
        return mismatches

    def verify_node_record(self, record: Mapping[str, Any]) -> None:
        mismatches = self._node_record_mismatches(record)
        if mismatches:
            raise WorkflowFilesModified(self.workflow_id, mismatches)

    def verify(self, *, include_service: bool = True) -> dict[str, Any]:
        """Compare FlowX-owned workflow files against their recorded SHA-256 digests."""
        modified: list[dict[str, Any]] = []
        if self.record.digests_tracked:
            for key, path in self._plan_entries():
                mismatch = self._check_digest(path, self.record.plan_digests.get(key))
                if mismatch is not None:
                    modified.append(mismatch)
        manifest = self.manifest()
        for registry in manifest.get("nodes", {}).values():
            versions = registry.get("versions") or []
            if versions:
                modified.extend(self._node_record_mismatches(versions[-1]))
        workflows = manifest.get("workflows") or []
        if workflows:
            latest = workflows[-1]
            digests = latest.get("digests")
            if isinstance(digests, Mapping):
                workflow_path, requirement_path = self.workflow_paths(latest)
                for key, path in (("workflow", workflow_path), ("requirement", requirement_path)):
                    if key in digests:
                        mismatch = self._check_digest(path, str(digests[key]))
                        if mismatch is not None:
                            modified.append(mismatch)
            for node_record in (latest.get("nodes") or {}).values():
                modified.extend(self._node_record_mismatches(node_record))
        if include_service and self.record.service_digests and self.record.main_entrypoint_path is not None:
            package_dir = self.record.service_dir
            for filename, expected in sorted(self.record.service_digests.items()):
                mismatch = self._check_digest(package_dir / filename, expected)
                if mismatch is not None:
                    modified.append(mismatch)
            if package_dir.is_dir():
                for path in sorted(package_dir.iterdir()):
                    if path.is_file() and (path.suffix == ".py" or path.name == "workflow.json"):
                        if path.name not in self.record.service_digests:
                            modified.append({
                                "path": str(path.relative_to(self.directory)),
                                "status": "unexpected",
                                "expected": None,
                                "actual": _file_digest(path),
                            })
        return {
            "ok": not modified,
            "workflow_id": self.workflow_id,
            "modified": modified,
            "remedy": "" if not modified else _INTEGRITY_REMEDY,
        }

    def read_artifact(
        self,
        slot: str,
        *,
        node: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Return verified read-only text for one FlowX-owned artifact."""
        if slot == "requirement":
            if version is not None:
                snapshot = self.workflow_snapshot(version)
                if snapshot is None:
                    raise KeyError(f"unknown workflow version: {version}")
                _, path = self.workflow_paths(snapshot)
                expected = (snapshot.get("digests") or {}).get("requirement")
                mismatch = self._check_digest(path, expected, allow_unexpected=expected is None)
                if mismatch is not None:
                    raise WorkflowFilesModified(self.workflow_id, [mismatch])
                return {"workflow_id": self.workflow_id, "slot": slot, "version": version,
                        "content": path.read_text(encoding="utf-8"), "read_only": True}
            return {"workflow_id": self.workflow_id, "slot": slot, "version": self.workflow_version(),
                    "content": self.read_requirement(), "read_only": True}
        if slot == "graph":
            if version is not None:
                snapshot = self.workflow_snapshot(version)
                if snapshot is None:
                    raise KeyError(f"unknown workflow version: {version}")
                path, _ = self.workflow_paths(snapshot)
                expected = (snapshot.get("digests") or {}).get("workflow")
                mismatch = self._check_digest(path, expected, allow_unexpected=expected is None)
                if mismatch is not None:
                    raise WorkflowFilesModified(self.workflow_id, [mismatch])
                return {"workflow_id": self.workflow_id, "slot": slot, "version": version,
                        "content": path.read_text(encoding="utf-8"), "read_only": True}
            self._verify_plan_file("graph", self.record.graph_path)
            if not self.record.graph_path.is_file():
                raise FileNotFoundError(f"workflow {self.workflow_id} has no workflow.json yet")
            return {"workflow_id": self.workflow_id, "slot": slot, "version": self.workflow_version(),
                    "content": self.record.graph_path.read_text(encoding="utf-8"), "read_only": True}
        if slot == "brief":
            return {"workflow_id": self.workflow_id, "slot": slot,
                    "content": self.read_brief(), "read_only": True}
        if slot in ("source", "test", "markdown", "log"):
            if not node:
                raise ValueError(f"node is required to read slot {slot!r}")
            manifest = self.manifest()
            nodes = manifest.get("nodes", {})
            resolved = node if node in nodes else next(
                (name for name, reg in nodes.items() if reg.get("node_id") == node), None,
            )
            if resolved is None:
                raise KeyError(f"unknown node: {node}")
            record = self.node_version(resolved, version) if version is not None else self.latest_node(resolved)
            if record is None:
                raise KeyError(f"unknown version {version} for node {resolved}")
            self.verify_node_record(record)
            path = self.artifacts_of(record).get(slot)
            if path is None or not path.is_file():
                raise FileNotFoundError(f"node {resolved} v{record['version']} has no {slot} artifact")
            return {
                "workflow_id": self.workflow_id, "slot": slot, "node": resolved,
                "node_id": record["node_id"], "version": record["version"],
                "content": path.read_text(encoding="utf-8"), "read_only": True,
            }
        raise ValueError("slot must be one of: requirement, graph, brief, source, test, markdown, log")


__all__ = ["MANIFEST_SCHEMA", "WorkflowFilesModified", "WorkflowRecord", "WorkflowStore", "artifact_targets"]
