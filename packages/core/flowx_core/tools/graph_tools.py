"""Utilities for working with node graphs defined in JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Set, Tuple

from pydaograph import GPipeline, GParam, register_class


PIPELINE_ID_GPARAM_KEY = "pipeline.id"


@register_class
class PipelineIdParam(GParam):
    """Shared pipeline identifier stored as a PyDaoGraph ``GParam``."""

    def __init__(self, value: str = "") -> None:
        super().__init__()
        self.value = str(value)


def set_pipeline_id(
    pipeline: GPipeline,
    pipeline_id: str,
    key: str = PIPELINE_ID_GPARAM_KEY,
):
    """Attach a pipeline identifier that every node can read at runtime.

    The identifier is stored as a pipeline-scoped ``GParam``. Nodes can then
    access it during ``run()`` with ``self.getGParam(key)`` or the convenience
    helper :func:`get_pipeline_id`.
    """

    return pipeline.createGParam(PipelineIdParam(pipeline_id), key)


def get_pipeline_id(owner: Any, key: str = PIPELINE_ID_GPARAM_KEY) -> str | None:
    """Read the shared pipeline identifier from a pipeline or node object."""

    if not hasattr(owner, "hasGParam") or not owner.hasGParam(key):
        return None

    param = owner.getGParam(key)
    value = getattr(param, "value", None)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _load_graph_json(source: Any) -> MutableMapping[str, Any]:
    """Load a graph JSON object from a mapping, JSON string, or file path."""

    if isinstance(source, Mapping):
        return dict(source)

    if isinstance(source, (str, Path)):
        path = Path(str(source))
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            return json.loads(str(source))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ValueError("Invalid JSON string provided") from exc

    raise TypeError("source must be a mapping, JSON string, or path-like object")


def graph_to_nodes(graph_json: Any) -> Dict[str, Dict[str, Any]]:
    """
    Convert a graph JSON structure into a node lookup map.

    The input can be a Python mapping, a JSON string, or a file path pointing to
    a JSON document with a top-level ``nodes`` list. Each entry in the returned
    dict uses the node name as the key and stores its ``type``, ``desc``, and
    ``depends`` fields.
    """

    graph_obj = _load_graph_json(graph_json)
    nodes = graph_obj.get("nodes", [])

    result: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            continue

        name = node.get("name")
        if not name:
            continue

        depends = node.get("depends", [])
        if isinstance(depends, (str, bytes)):
            depends = [depends]
        elif not isinstance(depends, list):
            depends = list(depends) if depends is not None else []

        result[name] = {
            "type": node.get("type", ""),
            "desc": node.get("desc", ""),
            "depends": depends,
            "ext_data": node.get("ext_data", ""),
        }

    return result


def is_dag(graph_json: Any) -> Tuple[bool, Iterable[str]]:
    """Check whether a graph JSON definition is a DAG.

    Returns a tuple of ``(is_acyclic, cycle_path)``. ``cycle_path`` will be an
    ordered iterable of node names describing one detected cycle when
    ``is_acyclic`` is False; otherwise it is empty.
    """

    nodes = graph_to_nodes(graph_json)

    # Include referenced-but-undefined nodes to trace cycles across dependencies.
    all_nodes: Set[str] = set(nodes)
    for info in nodes.values():
        for dep in info.get("depends", []) or []:
            all_nodes.add(str(dep))

    visiting: Set[str] = set()
    visited: Set[str] = set()
    cycle: List[str] = []

    def dfs(node: str, stack: list[str]) -> bool:
        if node in visiting:
            # Capture the cycle path from the first occurrence of ``node``.
            idx = stack.index(node)
            cycle.extend(stack[idx:] + [node])
            return False

        if node in visited:
            return True

        visiting.add(node)
        next_stack = stack + [node]
        deps = nodes.get(node, {}).get("depends", []) or []
        for dep in deps:
            dep_name = str(dep)
            if not dfs(dep_name, next_stack):
                return False

        visiting.remove(node)
        visited.add(node)
        return True

    for node_name in all_nodes:
        if node_name in visited:
            continue
        if not dfs(node_name, []):
            return False, cycle

    return True, []


def is_weakly_connected(graph_json: Any) -> Tuple[bool, List[List[str]]]:
    """Check whether all nodes belong to a single weakly connected component.

    Returns ``(is_connected, components)`` where ``components`` lists each
    connected component (direction ignored). A graph with zero nodes is treated
    as connected.
    """

    nodes = graph_to_nodes(graph_json)
    all_nodes: Set[str] = set(nodes)
    for info in nodes.values():
        for dep in info.get("depends", []) or []:
            all_nodes.add(str(dep))

    if not all_nodes:
        return True, []

    adjacency: Dict[str, Set[str]] = {name: set() for name in all_nodes}
    for name, info in nodes.items():
        for dep in info.get("depends", []) or []:
            dep_name = str(dep)
            adjacency[name].add(dep_name)
            adjacency.setdefault(dep_name, set()).add(name)

    components: List[List[str]] = []
    visited: Set[str] = set()

    for start in all_nodes:
        if start in visited:
            continue
        stack = [start]
        comp: List[str] = []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.append(node)
            stack.extend(adjacency.get(node, ()))
        components.append(comp)

    return len(components) == 1, components


__all__ = [
    "PIPELINE_ID_GPARAM_KEY",
    "PipelineIdParam",
    "set_pipeline_id",
    "get_pipeline_id",
    "graph_to_nodes",
    "is_dag",
    "is_weakly_connected",
]
