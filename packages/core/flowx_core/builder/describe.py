# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Added 2026-11-26: describe a built workflow's steps, input formats and input modes without guessing.
"""Read-only workflow descriptions for agents that are about to run a workflow."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from flowx_core.builder.store import WorkflowStore
from flowx_core.builder.verify import topological_order

_PLACEHOLDERS = {
    "string": "<string>", "number": 0, "integer": 0,
    "boolean": False, "object": {}, "array": [], "null": None,
}


def _source_info(store: WorkflowStore, name: str, record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    store.verify_node_record(record)
    source_path = store.artifacts_of(record).get("source")
    if not source_path or not source_path.is_file():
        return {}
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    node_class = next((s for s in tree.body if isinstance(s, ast.ClassDef) and s.name == name), None)
    if node_class is None:
        return {}
    attrs: dict[str, Any] = {}
    parses_json: bool | None = None
    for stmt in node_class.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Constant):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"TITLE", "PROMPT", "INPUT_REQUIRED"}:
                    attrs[target.id] = stmt.value.value
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "process_input":
            parses_json = any(
                (isinstance(c.func, ast.Attribute) and c.func.attr in {"loads", "literal_eval"})
                or (isinstance(c.func, ast.Name) and c.func.id in {"loads", "literal_eval"})
                for c in ast.walk(stmt) if isinstance(c, ast.Call)
            )
    return {
        "title": attrs.get("TITLE") or "",
        "prompt": attrs.get("PROMPT") or "",
        "input_required": attrs.get("INPUT_REQUIRED") if isinstance(attrs.get("INPUT_REQUIRED"), bool) else True,
        "parses_json": parses_json,
    }


def _input_plan(
    node: Mapping[str, Any], ext_type: str, fields: Mapping[str, str], source: Mapping[str, Any],
) -> tuple[bool, str, Any, str]:
    kind = node.get("meta_node_kind", "")
    input_required = source.get("input_required")
    if input_required is None:
        input_required = ext_type in {"user_input", "user_file_input"} or kind == "WorkflowFileNode"
    if not input_required:
        return False, "automatic", None, "Automatic node; it runs after its dependencies."
    if kind == "WorkflowFileNode" or ext_type == "user_file_input":
        return True, "file", {"file_path": "/path/to/file"}, 'Send `file_path` for one file or `{"files": [...]}` for several.'
    if len(fields) > 1 or any(t in {"object", "array"} for t in fields.values()):
        parses = source.get("parses_json")
        if parses is False:
            note = "Multi-field `inputs_format` not parsed in `process_input`; pass one combined text or repair the node."
        elif parses is True:
            note = "Send a JSON object matching `inputs_format`; `process_input` parses JSON."
        else:
            note = "Send a JSON object matching `inputs_format`; confirm `process_input` parses JSON."
        return True, "json", {k: _PLACEHOLDERS.get(v, "<value>") for k, v in fields.items()}, note
    if fields:
        field_name, field_type = next(iter(fields.items()))
        return True, "text", "<value>", f"Send `{field_name}` ({field_type}) as one string; do not wrap it in a dict."
    return True, "text", "<value>", "Send one string; `inputs_format` is empty."


def describe_workflow(store: WorkflowStore) -> dict[str, Any]:
    """Return the workflow purpose and per-step descriptions, input formats and input modes."""
    record = store.record
    graph = store.read_graph()
    brief = store.read_brief()
    requirement = store.read_requirement()
    published = store.latest_workflow()
    main = record.main_entrypoint_path
    nodes: list[dict[str, Any]] = []
    if graph is not None:
        raw_nodes = {n["name"]: n for n in graph.get("nodes", []) if isinstance(n, Mapping) and n.get("name")}
        for name in topological_order(graph):
            node = raw_nodes[name]
            raw_ext = node.get("ext_data")
            ext = (
                {"type": str(raw_ext.get("type") or "").strip(), "desc": str(raw_ext.get("desc") or "").strip()}
                if isinstance(raw_ext, Mapping) else {"type": str(raw_ext or "").strip(), "desc": ""}
            )
            raw_fmt = node.get("inputs_format")
            fields = (
                {str(k).strip(): str(v).strip().lower() for k, v in raw_fmt.items() if str(k).strip() and str(v).strip()}
                if isinstance(raw_fmt, Mapping) else {}
            )
            node_record = store.latest_node(name)
            source = _source_info(store, name, node_record)
            input_required, input_mode, input_example, input_note = _input_plan(node, ext["type"].lower(), fields, source)
            nodes.append({
                "step_id": name,
                "node_id": node_record["node_id"] if node_record else None,
                "version": int(node_record["version"]) if node_record else 0,
                "title": source.get("title") or name,
                "description": (node.get("desc") or ext["desc"]).strip(),
                "prompt": source.get("prompt") or ext["desc"],
                "meta_node_kind": node.get("meta_node_kind", ""),
                "ext_data": ext,
                "depends": list(node.get("depends") or []),
                "inputs_format": fields,
                "input_required": input_required,
                "input_mode": input_mode,
                "input_example": input_example,
                "input_note": input_note,
                "source_parses_json": source.get("parses_json"),
            })
    return {
        "workflow_id": record.workflow_id,
        "workflow_version": int(published["version"]) if published else 0,
        "read_only": True,
        "has_graph": graph is not None,
        "has_main": bool(main and main.is_file()),
        "brief": brief,
        "requirement": requirement,
        "description": brief.strip() or (requirement.strip().split("\n\n", 1)[0] if requirement.strip() else ""),
        "errors": dict(record.errors),
        "nodes": nodes,
    }


__all__ = ["describe_workflow"]
