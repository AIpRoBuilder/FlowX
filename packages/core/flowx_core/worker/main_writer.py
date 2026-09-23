"""Utilities for generating a PyDaoGraph entrypoint from a file template."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any, Mapping, Optional, Sequence

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools.runnable_gnode import RunnableGNode


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import MAX_TOKENS
from flowx_core.tools.text_tools import normalize_requirement_analysis_result


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
    requirement_analysis_result: dict[str, Any] | None


@dataclass
class PromptMainFileCoder(RunnableGNode):
    """Render an AG-UI lifecycle FastAPI backend entrypoint from a template."""

    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    system_prompt: str = ""
    timeout: float | None = None
    client: object | None = None
    session_marking_prompt: str = ""
    template_path: str = "worker/templates/pydaograph_main.py.tmpl"
    _template_source: str = field(init=False, repr=False)
    _render_context_by_target: dict[Path, _MainEntrypointRenderContext] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        RunnableGNode.__init__(self)
        template_file = ROOT_DIR / self.template_path
        if not template_file.exists():
            raise FileNotFoundError(f"Main entrypoint template not found: {template_file}")
        self._template_source = template_file.read_text(encoding="utf-8")

    @staticmethod
    def _write_text_file(text: str, file_path: Path, *, overwrite: bool) -> Path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if file_path.exists() and not overwrite:
            raise FileExistsError(f"File already exists and overwrite is False: {file_path}")
        file_path.write_text(text, encoding="utf-8")
        return file_path

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

    @staticmethod
    def _build_non_cron_sections() -> dict[str, str]:
        return {
            "__CRON_IMPORTS__": "",
            "__CRON_STATE__": "",
            "__CRON_MODELS__": "",
            "__CRON_HELPERS__": "",
            "__RESET_SESSION_EXTRAS__": "",
            "__EXECUTION_ROUTE__": dedent(
                '''
                @app.post("/api/run-step")
                def run_step(payload: RunStepInput) -> StreamingResponse:
                    engine = _get_engine(payload.sessionId)
                    step_meta = next(
                        (s for s in STEP_CHAIN if _meta_field(s, "id") == payload.stepId), None
                    )
                    if step_meta is None:
                        raise HTTPException(
                            status_code=404, detail=f"Unknown stepId: {payload.stepId}"
                        )

                    step_id = _meta_field(step_meta, "id")
                    ext_data = _meta_field(step_meta, "extData")
                    ext_type = ext_data.get("type") if isinstance(ext_data, Mapping) else None

                    if ext_type == "user_file_input":
                        if payload.file_path is not None:
                            normalized_input: Any = {"file_path": payload.file_path}
                        else:
                            normalized_input = payload.input
                    else:
                        normalized_input = payload.input

                    return StreamingResponse(
                        engine._run_step_events(step_id, normalized_input),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                    )
                '''
            ).strip(),
        }

    @staticmethod
    def _build_cron_sections(cron_meta: Mapping[str, Any]) -> dict[str, str]:
        task_type_literal = repr(str(cron_meta.get("task_type") or "cron"))
        cron_expression_literal = repr(cron_meta.get("crontab_expression"))

        cron_state = dedent(
            '''
            CRON_CONFIG = {
                "isCronTask": True,
                "taskType": __TASK_TYPE__,
                "crontabExpression": __CRON_EXPRESSION__,
            }
            CRON_TASKS: dict[str, asyncio.Task[None]] = {}
            CRON_STATUS: dict[str, dict[str, Any]] = {}
            '''
        ).strip()

        cron_models = dedent(
            '''


            class CronPreviewInput(BaseModel):
                crontabExpression: str


            class CronStartInput(BaseModel):
                sessionId: str | None = None
            '''
        ).rstrip()

        cron_helpers = dedent(
            '''


            def _validate_crontab_expression(crontab_expression: str) -> str:
                normalized = crontab_expression.strip()
                if not normalized:
                    raise ValueError("crontabExpression is required")
                croniter(normalized, datetime.now())
                return normalized


            def _preview_cron_runs(crontab_expression: str, *, count: int = 5) -> list[str]:
                normalized = _validate_crontab_expression(crontab_expression)
                iterator = croniter(normalized, datetime.now())
                return [iterator.get_next(datetime).isoformat() for _ in range(count)]
            '''
        ).rstrip()

        execution_route = dedent(
            '''
            @app.get("/api/cron-config")
            def get_cron_config() -> dict[str, Any]:
                return dict(CRON_CONFIG)


            @app.post("/api/cron-preview")
            def cron_preview(payload: CronPreviewInput) -> dict[str, Any]:
                try:
                    next_run_times = _preview_cron_runs(payload.crontabExpression)
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": str(exc),
                        "nextRunTimes": [],
                    }
                return {
                    "ok": True,
                    "error": None,
                    "nextRunTimes": next_run_times,
                }


            @app.post("/cron/start")
            def start_cron(payload: CronStartInput | None = None) -> dict[str, Any]:
                session_id = payload.sessionId if payload and payload.sessionId else "cron-default"
                expression = _validate_crontab_expression(CRON_CONFIG.get("crontabExpression") or "")
                engine = _get_engine(session_id)

                active_task = CRON_TASKS.get(session_id)
                if active_task is not None and not active_task.done():
                    status = CRON_STATUS.get(session_id, {})
                    return {
                        "ok": True,
                        "running": True,
                        "sessionId": session_id,
                        "taskType": CRON_CONFIG["taskType"],
                        "crontabExpression": expression,
                        "lastRunAt": status.get("lastRunAt"),
                        "nextRunAt": status.get("nextRunAt"),
                        "message": "Cron runner already active.",
                    }

                status = CRON_STATUS.setdefault(
                    session_id,
                    {
                        "running": True,
                        "sessionId": session_id,
                        "taskType": CRON_CONFIG["taskType"],
                        "crontabExpression": expression,
                        "lastRunAt": None,
                        "nextRunAt": None,
                    },
                )
                status.update(
                    {
                        "running": True,
                        "sessionId": session_id,
                        "taskType": CRON_CONFIG["taskType"],
                        "crontabExpression": expression,
                    }
                )

                async def _run_periodic() -> None:
                    try:
                        while True:
                            now = datetime.now()
                            next_run = croniter(expression, now).get_next(datetime)
                            status["nextRunAt"] = next_run.isoformat()
                            wait_seconds = max((next_run - now).total_seconds(), 0.0)
                            if wait_seconds:
                                await asyncio.sleep(wait_seconds)

                            events = engine._run_all_steps_events()
                            if hasattr(events, "__aiter__"):
                                async for _ in events:
                                    pass
                            else:
                                for _ in events:
                                    pass

                            status["lastRunAt"] = datetime.now().isoformat()
                    except asyncio.CancelledError:
                        raise
                    finally:
                        status["running"] = False
                        CRON_TASKS.pop(session_id, None)

                CRON_TASKS[session_id] = asyncio.create_task(_run_periodic())
                return {
                    "ok": True,
                    "running": True,
                    "sessionId": session_id,
                    "taskType": CRON_CONFIG["taskType"],
                    "crontabExpression": expression,
                    "lastRunAt": status.get("lastRunAt"),
                    "nextRunAt": status.get("nextRunAt"),
                }
            '''
        ).strip()

        reset_session_extras = "\n".join(
            f"    {line}" if line else line
            for line in dedent(
                '''
                cron_task = CRON_TASKS.pop(payload.sessionId, None)
                if cron_task is not None and not cron_task.done():
                    cron_task.cancel()
                CRON_STATUS.pop(payload.sessionId, None)
                '''
            ).strip().splitlines()
        )

        replace_map = {
            "__TASK_TYPE__": task_type_literal,
            "__CRON_EXPRESSION__": cron_expression_literal,
        }

        def _replace_placeholders(text: str) -> str:
            result = text
            for placeholder, value in replace_map.items():
                result = result.replace(placeholder, value)
            return result

        return {
            "__CRON_IMPORTS__": "import asyncio\nfrom datetime import datetime\n\nfrom croniter import croniter\n\n",
            "__CRON_STATE__": _replace_placeholders(cron_state),
            "__CRON_MODELS__": _replace_placeholders(cron_models),
            "__CRON_HELPERS__": _replace_placeholders(cron_helpers),
            "__RESET_SESSION_EXTRAS__": reset_session_extras,
            "__EXECUTION_ROUTE__": _replace_placeholders(execution_route),
        }

    def _build_main_source(self, context: _MainEntrypointRenderContext) -> str:
        cron_meta = normalize_requirement_analysis_result(context.requirement_analysis_result)
        sections = (
            self._build_cron_sections(cron_meta or {})
            if cron_meta and cron_meta["is_cron_task"]
            else self._build_non_cron_sections()
        )

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
            **sections,
        }

        source = self._template_source
        for placeholder, value in replacements.items():
            source = source.replace(placeholder, value)
        return source

    def _build_user_prompt(
        self,
        *,
        project_root_path: Path,
        graph_plan_json_path: Path,
        node_class_names: Sequence[str],
        nodes_package_name: str,
        fastapi_host: str,
        fastapi_port: int,
        uvicorn_reload: bool,
        requirement_analysis_result: Mapping[str, Any] | None = None,
    ) -> str:
        context = _MainEntrypointRenderContext(
            project_root_path=project_root_path,
            graph_plan_json_path=graph_plan_json_path,
            output_path=project_root_path / "main.py",
            node_class_names=tuple(node_class_names),
            nodes_package_name=nodes_package_name,
            fastapi_host=fastapi_host,
            fastapi_port=fastapi_port,
            uvicorn_reload=uvicorn_reload,
            requirement_analysis_result=(
                dict(requirement_analysis_result)
                if isinstance(requirement_analysis_result, Mapping)
                else None
            ),
        )
        return self._build_main_source(context)

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

        return self._write_text_file("\n".join(lines), init_path, overwrite=overwrite)

    def write_main_entrypoint(
        self,
        *,
        project_root_path: str,
        graph_plan_json_path: str,
        output_path: str,
        requirement_analysis_result: Mapping[str, Any] | None = None,
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
            requirement_analysis_result=(
                dict(requirement_analysis_result)
                if isinstance(requirement_analysis_result, Mapping)
                else None
            ),
        )

        source = self._build_main_source(context)
        written_path = self._write_text_file(source, target_path, overwrite=overwrite)
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
            requirement_analysis_result=previous_context.requirement_analysis_result,
        )

        source = self._build_main_source(refreshed_context)
        written_path = self._write_text_file(source, resolved_target, overwrite=overwrite)
        self._render_context_by_target[written_path.resolve()] = refreshed_context
        return written_path
