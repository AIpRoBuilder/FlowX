"""Collect graph-declared inputs and existing backend output formats."""

from __future__ import annotations

from typing import Any, Mapping

from flowx_core.tools.file_tools import compile_node_file_and_get_step_output_card_schema


def collect_node_formats(builder: Any, planned_graph: Any, backend_language: str = "python") -> dict[str, dict[str, Any]]:
    """Describe each node's user inputs and compiled output card, if present."""
    formats: dict[str, dict[str, Any]] = {}
    for node_name in planned_graph.get_topological_sorted_nodes():
        node_meta = planned_graph.get_node_meta(node_name)
        inputs = getattr(node_meta, "inputs_format", None) or {}
        if not isinstance(inputs, Mapping):
            inputs = {}
        ext_data = getattr(node_meta, "ext_data", None) or {}
        ext_type = (
            str(ext_data.get("type", "none")).strip().lower()
            if isinstance(ext_data, Mapping)
            else str(ext_data).strip().lower()
        )
        user_input_format = (
            {str(key).strip(): str(value).strip().lower()
             for key, value in inputs.items() if str(key).strip() and str(value).strip()}
            if ext_type in {"user_input", "skill"}
            else {}
        )
        backend_path_value = builder.node_location_map.get(node_name)
        backend_path = (
            builder._resolve_root_path(backend_path_value)
            if backend_path_value
            else builder._expected_backend_node_path(node_name, backend_language)
        )
        schema = None
        if backend_path.is_file():
            schema = compile_node_file_and_get_step_output_card_schema(str(backend_path))
        formats[node_name] = {
            "user_input_format": user_input_format,
            "backend_output_card_format": schema.get("card") if schema else None,
            "backend_node_path": str(backend_path) if backend_path.is_file() else None,
        }
    return formats
