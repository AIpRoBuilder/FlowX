# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: add graph diffing for evolve mode and verify published nodes during staging.
"""Deterministic checks a submitted artifact must pass before it is accepted."""

from __future__ import annotations

import json
import keyword
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.auditor import GraphJsonAuditor, NodeAuditor
from flowx_core.builder.findings import Finding, finding, from_violations
from flowx_core.builder.store import WorkflowStore, artifact_targets
from flowx_core.tools.artifact_tools import fingerprint_json

_RESERVED_MODULES = frozenset(name.casefold() for name in sys.stdlib_module_names) | {
    "pytest", "conftest", "ag_ui_workflow", "pydaograph", "flowx_core",
}
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_REQUIREMENT_SECTIONS = {
    "background": re.compile(r"^##\s+(background|需求背景)", re.IGNORECASE | re.MULTILINE),
    "goals": re.compile(r"^##\s+(goals?|objectives?|需求目标)", re.IGNORECASE | re.MULTILINE),
    "scope": re.compile(r"^##\s+(scope|需求范围)", re.IGNORECASE | re.MULTILINE),
    "functional details": re.compile(r"^##\s+(functional\s+details?|功能详细说明)", re.IGNORECASE | re.MULTILINE),
}


@dataclass
class Verdict:
    """Outcome of checking one submission."""

    accepted: bool
    findings: list[Finding] = field(default_factory=list)
    test: dict[str, Any] | None = None
    published: dict[str, Any] | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "findings": [item.to_dict() for item in self.findings],
            "test": self.test,
            "published": self.published,
            "message": self.message,
        }


def check_requirement(text: Any) -> list[Finding]:
    if not isinstance(text, str) or not text.strip():
        return [finding("requirement.non_empty", "The requirement analysis is empty.")]
    findings: list[Finding] = []
    missing = [name for name, pattern in _REQUIREMENT_SECTIONS.items() if not pattern.search(text)]
    if missing:
        findings.append(finding(
            "requirement.sections", "Missing sections: " + ", ".join(f"## {name.title()}" for name in missing),
        ))
    if not (re.search(r"meta_node_kind", text) and re.search(r"^\s*\|.*\|\s*$", text, re.MULTILINE)):
        findings.append(finding(
            "requirement.node_design",
            "No node design table with a `meta_node_kind` column was found in Functional Details.",
        ))
    return findings


def parse_graph(value: Any) -> tuple[dict[str, Any] | None, list[Finding]]:
    """Accept a graph object or its JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            return None, [finding("graph.json", f"workflow.json is not valid JSON: {exc.msg} (line {exc.lineno}).",
                                  line=exc.lineno)]
    if not isinstance(value, Mapping):
        return None, [finding("graph.json", "workflow.json must be a JSON object with a `nodes` array.")]
    return dict(value), []


def _cycle_free(dependencies: Mapping[str, list[str]]) -> bool:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str) -> bool:
        if name in done:
            return True
        if name in visiting:
            return False
        visiting.add(name)
        ok = all(visit(dep) for dep in dependencies.get(name, []) if dep in dependencies)
        visiting.discard(name)
        done.add(name)
        return ok

    return all(visit(name) for name in dependencies)


def validate_graph_structure(graph: Mapping[str, Any]) -> list[Finding]:
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return [finding("graph.json", "workflow.json needs a non-empty `nodes` array.")]
    findings: list[Finding] = []
    dependencies: dict[str, list[str]] = {}
    enabled: set[str] = set()
    folded: set[str] = set()
    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, Mapping):
            findings.append(finding("graph.json", f"nodes[{index - 1}] must be a JSON object.", line=index))
            continue
        name = node.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or keyword.iskeyword(name):
            findings.append(finding("graph.node_name", f"nodes[{index - 1}] has an invalid name {name!r}.", line=index))
            continue
        if name in dependencies:
            findings.append(finding("graph.node_name", f"Duplicate node name {name!r}.", line=index))
            continue
        if name.casefold() in folded:
            findings.append(finding("graph.node_name", f"Node {name!r} collides with another name ignoring case.", line=index))
        if name.casefold() in _RESERVED_MODULES:
            findings.append(finding("graph.node_name", f"Node {name!r} shadows a Python or runtime module.", line=index))
        folded.add(name.casefold())
        for key in ("type", "meta_node_kind", "desc"):
            if not isinstance(node.get(key), str) or not node[key].strip():
                findings.append(finding("graph.node_fields", f"Node {name!r} is missing `{key}`.", line=index))
        ext = node.get("ext_data")
        if not isinstance(ext, Mapping) or not ext.get("type") or "desc" not in ext:
            findings.append(finding("graph.node_fields", f"Node {name!r} needs `ext_data` {{type, desc}}.", line=index))
        depends = node.get("depends", [])
        if not isinstance(depends, list) or any(not isinstance(dep, str) for dep in depends):
            findings.append(finding("graph.dependencies", f"Node {name!r}: `depends` must be a list of names.", line=index))
            depends = []
        if len(set(depends)) != len(depends) or name in depends:
            findings.append(finding("graph.dependencies", f"Node {name!r} has duplicate or self dependencies.", line=index))
        if not isinstance(node.get("enable", True), bool):
            findings.append(finding("graph.node_fields", f"Node {name!r}: `enable` must be a boolean.", line=index))
        if node.get("loop", 1) != 1:
            findings.append(finding("graph.loop", f"Node {name!r}: `loop` must be 1.", line=index))
        dependencies[name] = list(depends)
        if node.get("enable", True) is True:
            enabled.add(name)
    for name, depends in dependencies.items():
        unknown = [dep for dep in depends if dep not in dependencies]
        if unknown:
            findings.append(finding("graph.dependencies", f"Node {name!r} depends on undefined nodes: {unknown}."))
        if name in enabled:
            disabled = [dep for dep in depends if dep in dependencies and dep not in enabled]
            if disabled:
                findings.append(finding("graph.enabled", f"Enabled node {name!r} depends on disabled nodes: {disabled}."))
    if dependencies and not enabled:
        findings.append(finding("graph.json", "At least one node must be enabled."))
    if not _cycle_free(dependencies):
        findings.append(finding("graph.cycle_detected", "The graph contains a dependency cycle."))
    return findings


def check_graph(graph: Mapping[str, Any], scratch: Path, auditor: GraphJsonAuditor) -> list[Finding]:
    """Structural rules plus the graph auditor, on a scratch copy of the document."""
    findings = validate_graph_structure(graph)
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "workflow.json"
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _, violations = auditor.audit_graph_json(Graph(str(path)))
    except Exception:
        return findings
    seen = {(item.rule_id, item.message) for item in findings}
    for item in from_violations(violations, namespace="graph", file="workflow.json"):
        if (item.rule_id, item.message) not in seen:
            findings.append(item)
    return findings


def topological_order(graph: Mapping[str, Any]) -> list[str]:
    """Enabled node names, parents first (ties keep file order)."""
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, Mapping)]
    enabled = [node["name"] for node in nodes if node.get("enable", True) is True]
    depends = {node["name"]: [dep for dep in node.get("depends", []) if dep in enabled] for node in nodes if node["name"] in enabled}
    order: list[str] = []
    placed: set[str] = set()
    while len(order) < len(enabled):
        ready = [name for name in enabled if name not in placed and all(dep in placed for dep in depends[name])]
        if not ready:
            raise ValueError("graph contains a cycle")
        for name in ready:
            order.append(name)
            placed.add(name)
    return order


def ancestors_of(graph: Mapping[str, Any], name: str) -> list[str]:
    """Transitive parents of a node in topological order, excluding the node itself."""
    parents = {node["name"]: list(node.get("depends", [])) for node in graph.get("nodes", []) if isinstance(node, Mapping)}
    wanted: set[str] = set()
    stack = list(parents.get(name, []))
    while stack:
        current = stack.pop()
        if current in wanted or current not in parents:
            continue
        wanted.add(current)
        stack.extend(parents[current])
    return [item for item in topological_order(graph) if item in wanted]


def diff_graphs(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two workflow graphs into added, removed, modified, rewired, unchanged, and rebuild sets."""
    before_nodes = {
        node["name"]: node for node in before.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("name"), str) and node.get("enable", True) is True
    }
    after_nodes = {
        node["name"]: node for node in after.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("name"), str) and node.get("enable", True) is True
    }
    order = topological_order(after)
    added = [name for name in order if name not in before_nodes]
    removed = [name for name in before_nodes if name not in after_nodes]
    modified: list[str] = []
    rewired: list[str] = []
    unchanged: list[str] = []
    for name in order:
        if name not in before_nodes:
            continue
        old, new = before_nodes[name], after_nodes[name]
        if fingerprint_json(old) == fingerprint_json(new):
            unchanged.append(name)
        else:
            modified.append(name)
            if list(old.get("depends", [])) != list(new.get("depends", [])):
                rewired.append(name)
    dirty = set(added) | set(modified)
    children: dict[str, list[str]] = {name: [] for name in order}
    for name in order:
        for dep in after_nodes[name].get("depends", []):
            if dep in children:
                children[dep].append(name)
    queue = list(dirty)
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in dirty:
                dirty.add(child)
                queue.append(child)
    rebuild = [name for name in order if name in dirty]
    changed = bool(added or removed or modified or fingerprint_json(before) != fingerprint_json(after))
    return {
        "changed": changed,
        "added": added,
        "removed": removed,
        "modified": modified,
        "rewired": rewired,
        "unchanged": unchanged,
        "rebuild": rebuild,
    }


def run_node_test(
    *,
    node_name: str,
    test_path: Path,
    log_path: Path,
    cwd: Path,
    timeout: int,
    python_command: str,
    run_subprocess: Callable[..., Any],
) -> dict[str, Any]:
    """Run one node's pytest module offline (media in dry-run mode) and write its log."""
    command = [python_command or sys.executable, "-m", "pytest", str(test_path), "-q"]
    environment = {**os.environ, "FLOWX_MEDIA_DRY_RUN": os.environ.get("FLOWX_MEDIA_DRY_RUN", "1")}
    started = datetime.now()
    returncode: int | None = None
    stdout = stderr = ""
    timed_out = False
    try:
        process = run_subprocess(
            command, cwd=str(cwd), env=environment, capture_output=True, text=True, timeout=timeout,
        )
        returncode, stdout, stderr = process.returncode, process.stdout or "", process.stderr or ""
        ok = returncode == 0
        status = "✓ Test completed successfully." if ok else f"✗ Test failed with return code {returncode}."
    except subprocess.TimeoutExpired:
        ok, timed_out = False, True
        status = f"✗ Test timed out after {timeout} seconds."
    except Exception as exc:
        ok = False
        stderr = f"{type(exc).__name__}: {exc}"
        status = f"✗ Test raised an exception: {stderr}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "=== Node Test Log ===",
        f"Node: {node_name}",
        f"Test started: {started:%Y-%m-%d %H:%M:%S}",
        f"Test ended: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Test file: {test_path}",
        f"Executing: {' '.join(command)}",
        f"Working directory: {cwd}",
        f"Return code: {returncode if returncode is not None else 'N/A'}",
        "",
    ]
    if stdout:
        parts += ["--- STDOUT ---", stdout, ""]
    if stderr:
        parts += ["--- STDERR ---", stderr, ""]
    parts.append(status)
    text = "\n".join(parts) + "\n"
    log_path.write_text(text, encoding="utf-8")
    return {"ok": ok, "returncode": returncode, "timed_out": timed_out, "log_path": str(log_path), "log": text}


def stage_node(
    store: WorkflowStore,
    scratch: Path,
    name: str,
    graph: Mapping[str, Any],
    texts: Mapping[str, str | None],
) -> dict[str, Path]:
    """Lay out a directory the auditor and pytest can use: graph, published nodes and the candidate."""
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    (scratch / "workflow.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    for other in topological_order(graph):
        if other == name:
            continue
        record = store.latest_node(other)
        if record is not None:
            store.verify_node_record(record)
        source = store.artifacts_of(record).get("source") if record else None
        if source is not None and source.is_file():
            shutil.copy2(source, scratch / f"{other}.py")
    targets = artifact_targets(scratch, name)
    for key, text in texts.items():
        if text is not None and key in targets:
            targets[key].parent.mkdir(parents=True, exist_ok=True)
            targets[key].write_text(text, encoding="utf-8")
    return targets


def check_node(
    *,
    name: str,
    raw_node: Mapping[str, Any],
    targets: Mapping[str, Path],
    graph_path: Path,
    auditor: NodeAuditor,
    require_tests: bool,
    timeout: int,
    python_command: str,
    run_subprocess: Callable[..., Any],
) -> tuple[list[Finding], dict[str, Any] | None]:
    """Audit the candidate source, then run its test; both reports are returned together."""
    findings: list[Finding] = []
    source = targets["source"]
    test = targets["test"]
    if not source.is_file():
        return [finding("node.source_missing", f"No `code` was submitted for {name}.")], None
    if require_tests and not test.is_file():
        findings.append(finding("node.test_missing", f"No `test` was submitted for {name}."))
    node_meta = NodeMeta.from_dict(dict(raw_node))
    try:
        _, violations = auditor.audit_file(str(source), node_meta=node_meta, graph_plan_path=str(graph_path))
    except Exception as exc:
        return [finding("builder.internal_error", f"{type(exc).__name__}: {exc}", file=source.name)], None
    findings += from_violations(violations, namespace="node", file=f"{name}.py")
    if any(item.rule_id in {"node.syntax_error", "node.compile_error"} for item in findings) or not test.is_file():
        return findings, None
    result = run_node_test(
        node_name=name, test_path=test, log_path=targets["log"], cwd=source.parent,
        timeout=timeout, python_command=python_command, run_subprocess=run_subprocess,
    )
    if not result["ok"]:
        text = result["log"]
        findings.append(finding(
            "node.test_timeout" if result["timed_out"] else "node.test_failed",
            text[-4000:], file=f"tests/test_{name}.py",
        ))
    return findings, result


def blame_node(log: str, node_names: Any) -> str | None:
    """The node a failing run log most plausibly belongs to (innermost traceback frame first)."""
    names = tuple(node_names)
    for stem in reversed(re.findall(r'File "[^"]*?([A-Za-z_]\w*)\.py"', log)):
        if stem in names:
            return stem
    mentions = [(match.start(), name) for name in names for match in re.finditer(rf"\b{re.escape(name)}\b", log)]
    return max(mentions)[1] if mentions else None


__all__ = [
    "Verdict",
    "ancestors_of",
    "blame_node",
    "check_graph",
    "check_node",
    "check_requirement",
    "diff_graphs",
    "parse_graph",
    "run_node_test",
    "stage_node",
    "topological_order",
    "validate_graph_structure",
]
