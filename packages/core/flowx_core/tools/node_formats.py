"""Collect graph-declared inputs and existing backend output formats."""
# Modified 2026-10-28: resolve backend paths from an explicit root; AgentBuilder no longer tracks them.

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from flowx_core.tools.agent_builder_tools import get_language_extension
from flowx_core.tools.file_tools import compile_node_file_and_get_step_output_card_schema


def _resolve_node_path(root: Path, path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def collect_node_formats(
    planned_graph: Any,
    *,
    root_dir: str | Path,
    node_location_map: Mapping[str, str] | None = None,
    backend_language: str = "python",
) -> dict[str, dict[str, Any]]:
    """Describe each node's user inputs and compiled output card, if present."""
    root = Path(root_dir).expanduser().resolve()
    locations = node_location_map or {}
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
            if ext_type == "user_input"
            else {}
        )
        backend_path_value = locations.get(node_name)
        backend_path = (
            _resolve_node_path(root, backend_path_value)
            if backend_path_value
            else (root / f"{node_name}{get_language_extension(backend_language)}").resolve()
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
