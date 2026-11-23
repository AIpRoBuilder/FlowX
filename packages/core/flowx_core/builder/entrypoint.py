# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Deterministic rendering of a workflow service entrypoint from published nodes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "pydaograph_main.py.tmpl"


def enabled_node_names(graph: Mapping[str, Any]) -> list[str]:
    nodes = graph.get("nodes", []) if isinstance(graph, Mapping) else []
    if not isinstance(nodes, list):
        return []
    return [
        str(node.get("name", "")).strip()
        for node in nodes
        if isinstance(node, Mapping) and node.get("enable", True) is not False and str(node.get("name", "")).strip()
    ]


@dataclass(frozen=True)
class _Context:
    project_root: Path
    graph_json: Path
    output: Path
    node_names: tuple[str, ...]
    package_name: str
    host: str
    port: int
    reload: bool


class EntrypointWriter:
    """Render ``main.py`` and the node package ``__init__`` for one service package."""

    def __init__(self, template_path: Path | None = None) -> None:
        template = template_path or TEMPLATE_PATH
        if not template.is_file():
            raise FileNotFoundError(f"Main entrypoint template not found: {template}")
        self._template = template.read_text(encoding="utf-8")

    @staticmethod
    def _path_expression(base: str, *, base_dir: Path, target: Path) -> str:
        try:
            relative = Path(os.path.relpath(target, start=base_dir))
        except ValueError:
            return f"Path({str(target)!r})"
        if relative == Path("."):
            return base
        expression = base
        for part in relative.parts:
            if part in ("", "."):
                continue
            expression = f"{expression}.parent" if part == ".." else f"{expression} / {part!r}"
        return expression

    @staticmethod
    def _modules(package_dir: Path, ignored: set[str]) -> list[str]:
        ignored = {"__init__", *ignored}
        return [
            path.stem for path in sorted(package_dir.glob("*.py"))
            if not path.stem.startswith("_") and path.stem not in ignored
        ]

    def _node_names(self, graph: Mapping[str, Any], package_dir: Path, ignored: set[str]) -> list[str]:
        preferred = enabled_node_names(graph)
        discovered = set(self._modules(package_dir, ignored))
        if preferred:
            missing = [name for name in preferred if name not in discovered]
            if missing:
                raise FileNotFoundError(
                    f"Node files missing under package directory {package_dir}: {', '.join(missing)}"
                )
            return preferred
        if not discovered:
            raise ValueError(f"No node Python files found under package directory: {package_dir}")
        names = sorted(discovered)
        return [name for name in names if name[:1].isupper()] or names

    def write_package_init(self, package_dir: Path, node_names: Sequence[str]) -> Path:
        lines = ['"""Node package exports generated from workflow.json."""', ""]
        lines += [f"from .{name} import {name}" for name in node_names]
        lines += ["", "__all__ = [", *[f'    "{name}",' for name in node_names], "]", ""]
        path = package_dir / "__init__.py"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _render(self, context: _Context) -> str:
        imports = "\n".join(f"    {name}," for name in context.node_names)
        replacements = {
            "__NODES_PACKAGE_NAME__": context.package_name,
            "__NODE_IMPORTS__": imports,
            "__NODE_CLASS_ITEMS__": imports,
            "__APP_NAME__": context.package_name.replace("_", "-"),
            "__PIPELINE_JSON_EXPR__": self._path_expression(
                "_HERE", base_dir=context.output.parent, target=context.graph_json),
            "__PROJECT_ROOT_EXPR__": self._path_expression(
                "_HERE", base_dir=context.output.parent, target=context.project_root),
            "__STEP_CHAIN_ITEMS__": "\n".join(f"    {name}.step_meta()," for name in context.node_names),
            "__UVICORN_HOST__": repr(context.host),
            "__UVICORN_PORT__": str(context.port),
            "__UVICORN_RELOAD__": repr(context.reload),
        }
        source = self._template
        for placeholder, value in replacements.items():
            source = source.replace(placeholder, value)
        return source

    def write(
        self,
        *,
        project_root: Path,
        graph_json: Path,
        output: Path,
        fastapi_host: str = "0.0.0.0",
        fastapi_port: int = 8000,
        uvicorn_reload: bool = False,
    ) -> Path:
        project_root, graph_json, output = (Path(path).expanduser().resolve() for path in (project_root, graph_json, output))
        if not project_root.is_dir():
            raise FileNotFoundError(f"project_root does not exist or is not a directory: {project_root}")
        if not graph_json.is_file():
            raise FileNotFoundError(f"graph_json does not exist: {graph_json}")
        output.parent.mkdir(parents=True, exist_ok=True)
        graph = json.loads(graph_json.read_text(encoding="utf-8"))
        names = self._node_names(graph, output.parent, {output.stem})
        self.write_package_init(output.parent, names)
        context = _Context(
            project_root=project_root, graph_json=graph_json, output=output, node_names=tuple(names),
            package_name=output.parent.name, host=fastapi_host, port=int(fastapi_port), reload=bool(uvicorn_reload),
        )
        output.write_text(self._render(context), encoding="utf-8")
        return output


__all__ = ["EntrypointWriter", "TEMPLATE_PATH", "enabled_node_names"]
