"""Utilities for generating a PyDaoGraph entrypoint from a file template."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS


def _stringify_modules(module_names: Optional[Sequence[str]]) -> str:
    if not module_names:
        return "No explicit module list; auto-discover every Python file under nodes_root."
    return "Explicit node modules to import in order: " + ", ".join(module_names)


def _extract_enabled_node_class_names(graph_plan: Mapping[str, Any]) -> list[str]:
    nodes = graph_plan.get("nodes", []) if isinstance(graph_plan, Mapping) else []
    if not isinstance(nodes, list):
        return []

    node_class_names: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        if node.get("enable", True) is False:
            continue

        class_name = str(node.get("name", "")).strip()
        if class_name:
            node_class_names.append(class_name)
    return node_class_names


@dataclass
class _MainEntrypointRenderContext:
    project_root_path: Path
    graph_plan_json_path: Path
    output_path: Path
    node_class_names: tuple[str, ...]
    nodes_package_name: str
    fastapi_host: str
    fastapi_port: int
    uvicorn_reload: bool


@dataclass
class PromptMainFileCoder(Coder):
    """Render an AG-UI lifecycle FastAPI backend entrypoint from a template."""

    INPUT_REQUIRED = False

    system_prompt: str = ""
    template_path: str = "worker/templates/pydaograph_main.py.tmpl"
    _template_source: str = field(init=False, repr=False)
    _render_context_by_target: dict[Path, _MainEntrypointRenderContext] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        template_file = ROOT_DIR / self.template_path
        if not template_file.exists():
            raise FileNotFoundError(f"Main entrypoint template not found: {template_file}")
        self._template_source = template_file.read_text(encoding="utf-8")
        # Entrypoint generation is template-only and must not require LLM credentials.
        super().__post_init__(initialize_client=False)

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        """Run a write/amend request from session_state using upstream graph output if needed."""
        del user_input
        request = session_state.get("main_entrypoint")
        if not isinstance(request, Mapping):
            raise ValueError("session_state['main_entrypoint'] must be a write or amend request")

        action_key = f"{type(self).__name__}::action"
        action = session_state.get(action_key)
        output_path = request.get("output_path")
        if action not in ("write", "amend"):
            raise ValueError(f"{action_key} must be 'write' or 'amend'")
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("main_entrypoint output_path must be a non-empty string")

        if action == "write":
            graph_path = request.get("graph_plan_json_path")
            if not graph_path:
                graph_paths = {
                    result.derived.get("graph_plan_json_path")
                    for result in dependency_results.values()
                    if isinstance(result.derived, dict)
                    and isinstance(result.derived.get("graph_plan_json_path"), str)
                    and result.derived["graph_plan_json_path"]
                }
                if len(graph_paths) != 1:
                    raise ValueError("write requires exactly one graph_plan_json_path from the request or dependencies")
                graph_path = graph_paths.pop()
            if not isinstance(graph_path, str) or not graph_path.strip():
                raise ValueError("graph_plan_json_path must be a non-empty string")
            project_root = request.get("project_root_path")
            if not isinstance(project_root, str) or not project_root.strip():
                raise ValueError("write requires a non-empty project_root_path")
            written = self.write_main_entrypoint(
                project_root_path=project_root,
                graph_plan_json_path=graph_path,
                output_path=output_path,
                fastapi_host=request.get("fastapi_host", "0.0.0.0"),
                fastapi_port=request.get("fastapi_port", 8000),
                uvicorn_reload=request.get("uvicorn_reload", False),
                overwrite=request.get("overwrite", True),
                temperature=request.get("temperature", 0.2),
            )
        else:
            amendment = request.get("amendment")
            if not isinstance(amendment, str) or not amendment.strip():
                raise ValueError("amend requires a non-empty amendment")
            written = self.amend_code_with_feedback(
                output_path,
                amendment,
                language=request.get("language", "python"),
                overwrite=request.get("overwrite", True),
                temperature=request.get("temperature", 0.2),
            )
        return StepRunOutput(derived={"main_entrypoint_path": str(written)})

    @staticmethod
    def _render_relative_path_expr(base_expr: str, *, base_dir: Path, target_path: Path) -> str:
        try:
            relative = Path(os.path.relpath(target_path, start=base_dir))
        except ValueError:
            return f"Path({str(target_path)!r})"

        if relative == Path("."):
            return base_expr

        expression = base_expr
        for part in relative.parts:
            if part in ("", "."):
                continue
            if part == "..":
                expression = f"{expression}.parent"
            else:
                expression = f"{expression} / {part!r}"
        return expression

    @staticmethod
    def _list_python_module_names(
        package_dir: Path,
        *,
        ignored_module_names: set[str] | None = None,
    ) -> list[str]:
        ignored = {"__init__"}
        if ignored_module_names:
            ignored.update(ignored_module_names)

        module_names: list[str] = []
        for module_path in sorted(package_dir.glob("*.py")):
            module_name = module_path.stem
            if module_name.startswith("_") or module_name in ignored:
                continue
            module_names.append(module_name)
        return module_names

    def _resolve_node_class_names(
        self,
        *,
        graph_plan: Mapping[str, Any],
        package_dir: Path,
        ignored_module_names: set[str] | None = None,
    ) -> list[str]:
        preferred_names = _extract_enabled_node_class_names(graph_plan)
        discovered_names = self._list_python_module_names(
            package_dir,
            ignored_module_names=ignored_module_names,
        )
        discovered_lookup = set(discovered_names)

        if preferred_names:
            missing_node_files = [name for name in preferred_names if name not in discovered_lookup]
            if missing_node_files:
                missing_preview = ", ".join(missing_node_files)
                raise FileNotFoundError(
                    f"Node files missing under package directory {package_dir}: {missing_preview}"
                )
            return preferred_names

        if not discovered_names:
            raise ValueError(f"No node Python files found under package directory: {package_dir}")

        capitalized_names = [name for name in discovered_names if name[:1].isupper()]
        return capitalized_names or discovered_names

    @staticmethod
    def _format_node_imports(node_class_names: Sequence[str]) -> str:
        return "\n".join(f"    {class_name}," for class_name in node_class_names)

    @staticmethod
    def _format_step_chain_items(node_class_names: Sequence[str]) -> str:
        return "\n".join(f"    {class_name}.step_meta()," for class_name in node_class_names)

    def _build_main_source(self, context: _MainEntrypointRenderContext) -> str:
        replacements = {
            "__NODES_PACKAGE_NAME__": context.nodes_package_name,
            "__NODE_IMPORTS__": self._format_node_imports(context.node_class_names),
            "__APP_NAME__": context.nodes_package_name.replace("_", "-"),
            "__PIPELINE_JSON_EXPR__": self._render_relative_path_expr(
                "_HERE",
                base_dir=context.output_path.parent,
                target_path=context.graph_plan_json_path,
            ),
            "__PROJECT_ROOT_EXPR__": self._render_relative_path_expr(
                "_HERE",
                base_dir=context.output_path.parent,
                target_path=context.project_root_path,
            ),
            "__STEP_CHAIN_ITEMS__": self._format_step_chain_items(context.node_class_names),
            "__UVICORN_HOST__": repr(context.fastapi_host),
            "__UVICORN_PORT__": str(context.fastapi_port),
            "__UVICORN_RELOAD__": repr(context.uvicorn_reload),
        }

        source = self._template_source
        for placeholder, value in replacements.items():
            source = source.replace(placeholder, value)
        return source

    def write_nodes_package_init(
        self,
        *,
        graph_plan_json_path: str,
        package_dir_path: str,
        output_path: Optional[str] = None,
        overwrite: bool = True,
    ) -> Path:
        graph_plan_path = Path(graph_plan_json_path).expanduser().resolve()
        if not graph_plan_path.exists():
            raise FileNotFoundError(f"graph_plan_json_path does not exist: {graph_plan_path}")

        package_dir = Path(package_dir_path).expanduser().resolve()
        if not package_dir.exists() or not package_dir.is_dir():
            raise FileNotFoundError(f"package_dir_path does not exist or is not a directory: {package_dir}")

        graph_plan = json.loads(graph_plan_path.read_text(encoding="utf-8"))
        node_class_names = self._resolve_node_class_names(
            graph_plan=graph_plan,
            package_dir=package_dir,
        )

        init_path = Path(output_path).expanduser() if output_path else package_dir / "__init__.py"
        if init_path.exists() and not overwrite:
            raise FileExistsError(f"Output file already exists and overwrite=False: {init_path}")

        lines: list[str] = [
            '"""Node package exports generated from graph_plan.json."""',
            "",
        ]
        for class_name in node_class_names:
            lines.append(f"from .{class_name} import {class_name}")

        lines.extend(
            [
                "",
                "__all__ = [",
                *[f'    "{class_name}",' for class_name in node_class_names],
                "]",
                "",
            ]
        )

        return self.write_code_to_file("\n".join(lines), str(init_path), overwrite=overwrite)

    def write_main_entrypoint(
        self,
        *,
        project_root_path: str,
        graph_plan_json_path: str,
        output_path: str,
        fastapi_host: str = "0.0.0.0",
        fastapi_port: int = 8000,
        uvicorn_reload: bool = False,
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        del temperature, max_tokens

        project_root = Path(project_root_path).expanduser().resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise FileNotFoundError(f"project_root_path does not exist or is not a directory: {project_root}")

        graph_plan_path = Path(graph_plan_json_path).expanduser().resolve()
        if not graph_plan_path.exists():
            raise FileNotFoundError(f"graph_plan_json_path does not exist: {graph_plan_path}")

        target_path = Path(output_path).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

        graph_plan = json.loads(graph_plan_path.read_text(encoding="utf-8"))
        node_class_names = self._resolve_node_class_names(
            graph_plan=graph_plan,
            package_dir=target_path.parent,
            ignored_module_names={target_path.stem},
        )

        self.write_nodes_package_init(
            graph_plan_json_path=str(graph_plan_path),
            package_dir_path=str(target_path.parent),
            overwrite=overwrite,
        )

        context = _MainEntrypointRenderContext(
            project_root_path=project_root,
            graph_plan_json_path=graph_plan_path,
            output_path=target_path,
            node_class_names=tuple(node_class_names),
            nodes_package_name=target_path.parent.name,
            fastapi_host=fastapi_host,
            fastapi_port=fastapi_port,
            uvicorn_reload=uvicorn_reload,
        )

        source = self._build_main_source(context)
        written_path = self.write_code_to_file(source, str(target_path), overwrite=overwrite)
        self._render_context_by_target[written_path.resolve()] = context
        return written_path

    def amend_code_with_feedback(
        self,
        code_path: str,
        amendment: str,
        *,
        language: str = "python",
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        """Regenerate main.py from the stored template context."""

        del amendment, temperature, max_tokens

        language_clean = language.strip().lower() if language else "python"
        ext_map = {
            "python": ".py",
            "py": ".py",
            "javascript": ".js",
            "js": ".js",
            "typescript": ".ts",
            "ts": ".ts",
            "java": ".java",
            "go": ".go",
            "golang": ".go",
            "csharp": ".cs",
            "c#": ".cs",
        }

        target_path = Path(code_path)
        target_ext = ext_map.get(
            language_clean,
            f".{language_clean}" if language_clean else (target_path.suffix or ".txt"),
        )
        if target_path.suffix.lower() != target_ext:
            target_path = target_path.with_suffix(target_ext)

        if not target_path.exists():
            raise FileNotFoundError(f"Code file not found: {target_path}")

        resolved_target = target_path.expanduser().resolve()
        previous_context = self._render_context_by_target.get(resolved_target)
        if previous_context is None:
            raise ValueError(
                "No cached render context found for main entrypoint amendment. "
                "Call write_main_entrypoint(...) with this PromptMainFileCoder instance first."
            )

        graph_plan = json.loads(previous_context.graph_plan_json_path.read_text(encoding="utf-8"))
        node_class_names = self._resolve_node_class_names(
            graph_plan=graph_plan,
            package_dir=resolved_target.parent,
            ignored_module_names={resolved_target.stem},
        )

        refreshed_context = _MainEntrypointRenderContext(
            project_root_path=previous_context.project_root_path,
            graph_plan_json_path=previous_context.graph_plan_json_path,
            output_path=resolved_target,
            node_class_names=tuple(node_class_names),
            nodes_package_name=previous_context.nodes_package_name,
            fastapi_host=previous_context.fastapi_host,
            fastapi_port=previous_context.fastapi_port,
            uvicorn_reload=previous_context.uvicorn_reload,
        )

        source = self._build_main_source(refreshed_context)
        written_path = self.write_code_to_file(source, str(resolved_target), overwrite=overwrite)
        self._render_context_by_target[written_path.resolve()] = refreshed_context
        return written_path
