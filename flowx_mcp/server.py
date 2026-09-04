"""FastMCP server that wraps ``meta_agent`` and ``ag_ui_workflow`` workflows."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import importlib
import io
import json
import logging
import mimetypes
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .bootstrap import bootstrap_import_paths, load_env_from_runtime_context

load_env_from_runtime_context(__file__)
bootstrap_import_paths(__file__)

from .client import DEFAULT_RUN_TIMEOUT, health_check, run_step_sse
from .registry import find_free_port, meta_agent_available, meta_agent_import_error, registry

logger = logging.getLogger("flowx.server")

_MCP_SERVER_AVAILABLE = False
_MCP_SERVER_IMPORT_ERROR = ""
MCPServerClass: Any = None
for module_name, attr_name in (
    ("mcp.server.fastmcp", "FastMCP"),
    ("mcp.server.mcpserver", "MCPServer"),
):
    try:
        MCPServerClass = getattr(importlib.import_module(module_name), attr_name)
        _MCP_SERVER_AVAILABLE = True
        break
    except Exception as exc:
        _MCP_SERVER_IMPORT_ERROR = str(exc)
if not _MCP_SERVER_AVAILABLE:
    MCPServerClass = None


def _json_default(value: Any) -> Any:
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)


_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
def _error(tool: str, message: str, **extra: Any) -> str:
    payload = {
        "ok": False,
        "tool": tool,
        "error": message,
    }
    payload.update(extra)
    return _json(payload)


def _default_workspace() -> str:
    workspace = os.environ.get("FLOWX_DEFAULT_WORKSPACE")
    if not workspace:
        workspace = str((Path.cwd() / ".flowx_workspaces").resolve())
    path = Path(workspace).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _resolve_workspace(workspace: Optional[str]) -> str:
    if isinstance(workspace, str) and workspace.strip():
        path = Path(workspace).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    return _default_workspace()


def _decode_base64_file_content(content_base64: str) -> bytes:
    payload = str(content_base64 or "").strip()
    if not payload:
        raise ValueError("content_base64 must be a non-empty base64 string")
    lowered = payload.lower()
    if lowered.startswith("data:") and ";base64," in lowered:
        payload = payload.split(",", 1)[1]
    normalized = "".join(payload.split())
    try:
        return base64.b64decode(normalized, validate=True)
    except binascii.Error as exc:
        raise ValueError("content_base64 is not valid base64-encoded file content") from exc


def _write_workspace_input_file(
    *,
    file_name: str,
    content_base64: str,
    workspace: Optional[str] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    workspace_dir = Path(_resolve_workspace(workspace)).resolve()
    inputs_dir = (workspace_dir / "inputs").resolve()
    inputs_dir.mkdir(parents=True, exist_ok=True)

    requested_name = _normalize_name(file_name, "file_name")
    if Path(requested_name).name != requested_name:
        raise ValueError("file_name must be a plain file name under workspace/inputs")

    target_path = (inputs_dir / requested_name).resolve()
    try:
        target_path.relative_to(inputs_dir)
    except ValueError as exc:
        raise ValueError("file_name must resolve inside workspace/inputs") from exc
    if target_path.exists() and target_path.is_dir():
        raise ValueError(f"file_name '{requested_name}' resolves to a directory")

    existed = target_path.is_file()
    if existed and not overwrite:
        raise FileExistsError(
            f"input file '{requested_name}' already exists under {inputs_dir}; "
            "pass overwrite=true to replace it"
        )

    content = _decode_base64_file_content(content_base64)
    target_path.write_bytes(content)
    mime_type, _ = mimetypes.guess_type(target_path.name)
    return {
        "workspace": str(workspace_dir),
        "inputs_dir": str(inputs_dir),
        "file_name": target_path.name,
        "relative_path": target_path.relative_to(workspace_dir).as_posix(),
        "file_path": str(target_path),
        "mime_type": mime_type or "application/octet-stream",
        "size_bytes": len(content),
        "overwritten": existed,
    }


def _workspace_relative_path(workspace_dir: Path, path: Path) -> str:
    return path.relative_to(workspace_dir).as_posix()


def _resolve_requested_workspace_file(workspace_dir: Path, file_name: str) -> Path:
    requested_name = _normalize_name(file_name, "file_name")
    direct_path = (workspace_dir / requested_name).resolve()
    try:
        direct_path.relative_to(workspace_dir)
    except ValueError as exc:
        raise ValueError(f"file_name '{requested_name}' escapes the workspace directory") from exc
    if direct_path.is_file():
        return direct_path

    matches: list[Path] = []
    for path in workspace_dir.rglob("*"):
        if not path.is_file() or path.name != requested_name:
            continue
        resolved_path = path.resolve()
        try:
            resolved_path.relative_to(workspace_dir)
        except ValueError:
            continue
        matches.append(resolved_path)

    matches.sort(key=lambda path: _workspace_relative_path(workspace_dir, path).lower())
    if not matches:
        raise FileNotFoundError(f"file '{requested_name}' was not found under workspace '{workspace_dir}'")
    if len(matches) > 1:
        match_list = ", ".join(_workspace_relative_path(workspace_dir, path) for path in matches)
        raise ValueError(
            f"file name '{requested_name}' is ambiguous under workspace '{workspace_dir.name}': "
            f"{match_list}. Pass a relative path instead."
        )
    return matches[0]


def _delete_workspace_files_by_name(
    file_names: list[str],
    workspace: Optional[str] = None,
) -> list[dict[str, str]]:
    workspace_dir = Path(_resolve_workspace(workspace)).resolve()
    deleted_files: list[dict[str, str]] = []
    for requested_name in file_names:
        path = _resolve_requested_workspace_file(workspace_dir, requested_name)
        path.unlink()
        deleted_files.append(
            {
                "requested_name": requested_name,
                "file_name": path.name,
                "relative_path": _workspace_relative_path(workspace_dir, path),
            }
        )
    return deleted_files


def _discover_workflow_folders(root_dir: str) -> list[dict[str, Any]]:
    workspace_dir = Path(root_dir).expanduser().resolve()
    discovered: list[dict[str, Any]] = []
    for child in sorted(workspace_dir.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        has_graph_plan = (child / "graph_plan.json").is_file()
        has_workflow_json = (child / "workflow.json").is_file()
        if not has_graph_plan and not has_workflow_json:
            continue
        discovered.append(
            {
                "workflow_name": child.name,
                "root_dir": str(child),
                "has_graph_plan": has_graph_plan,
                "has_workflow_json": has_workflow_json,
            }
        )
    return discovered


def _resolve_workflow_root(workflow_name: str, workspace: Optional[str] = None) -> Path:
    workspace_dir = Path(_resolve_workspace(workspace)).resolve()
    workflow_root = (workspace_dir / _normalize_name(workflow_name, "workflow_name")).resolve()
    try:
        workflow_root.relative_to(workspace_dir)
    except ValueError as exc:
        raise ValueError("workflow_name must resolve inside the workspace directory") from exc
    if not workflow_root.is_dir():
        raise KeyError(f"workflow '{workflow_name}' does not exist at {workflow_root}")
    return workflow_root


def _workflow_relative_path(workflow_root: Path, path: Path) -> str:
    return path.relative_to(workflow_root).as_posix()


def _resolve_requested_workflow_file(workflow_root: Path, file_name: str) -> Path:
    requested_name = _normalize_name(file_name, "file_name")
    direct_path = (workflow_root / requested_name).resolve()
    try:
        direct_path.relative_to(workflow_root)
    except ValueError as exc:
        raise ValueError(f"file_name '{requested_name}' escapes the workflow directory") from exc
    if direct_path.is_file():
        return direct_path

    matches = sorted(
        [path for path in workflow_root.rglob("*") if path.is_file() and path.name == requested_name],
        key=lambda path: _workflow_relative_path(workflow_root, path).lower(),
    )
    if not matches:
        raise FileNotFoundError(
            f"file '{requested_name}' was not found under workflow '{workflow_root.name}'"
        )
    if len(matches) > 1:
        match_list = ", ".join(_workflow_relative_path(workflow_root, path) for path in matches)
        raise ValueError(
            f"file name '{requested_name}' is ambiguous under workflow '{workflow_root.name}': "
            f"{match_list}. Pass a relative path instead."
        )
    return matches[0]


def _list_python_workflow_files(workflow_root: Path) -> list[dict[str, str]]:
    python_files = [path for path in workflow_root.rglob("*.py") if path.is_file()]
    return [
        {
            "file_name": path.name,
            "relative_path": _workflow_relative_path(workflow_root, path),
        }
        for path in sorted(
            python_files,
            key=lambda path: _workflow_relative_path(workflow_root, path).lower(),
        )
    ]


def _get_workflow_files_by_name(
    workflow_root: Path,
    file_names: list[str],
) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for requested_name in file_names:
        path = _resolve_requested_workflow_file(workflow_root, requested_name)
        files.append(
            {
                "requested_name": requested_name,
                "file_name": path.name,
                "relative_path": _workflow_relative_path(workflow_root, path),
                "content": path.read_text(encoding="utf-8"),
            }
        )
    return files


def _get_workflow_binary_files_by_name(
    workflow_root: Path,
    file_names: list[str],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for requested_name in file_names:
        path = _resolve_requested_workflow_file(workflow_root, requested_name)
        content = path.read_bytes()
        mime_type, _ = mimetypes.guess_type(path.name)
        files.append(
            {
                "requested_name": requested_name,
                "file_name": path.name,
                "relative_path": _workflow_relative_path(workflow_root, path),
                "mime_type": mime_type or "application/octet-stream",
                "size_bytes": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
    return files


def _replace_workflow_files_by_name(
    workflow_root: Path,
    file_names: list[str],
    new_file_contents: list[str],
) -> list[dict[str, str]]:
    if len(file_names) != len(new_file_contents):
        raise ValueError("file_names and new_file_contents must have the same length")

    updated_files: list[dict[str, str]] = []
    for requested_name, new_content in zip(file_names, new_file_contents):
        path = _resolve_requested_workflow_file(workflow_root, requested_name)
        path.write_text(str(new_content), encoding="utf-8")
        updated_files.append(
            {
                "requested_name": requested_name,
                "file_name": path.name,
                "relative_path": _workflow_relative_path(workflow_root, path),
            }
        )
    return updated_files


def _normalize_name(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must be a non-empty string")
    return normalized


@contextlib.contextmanager
def _capture_builder_output(label: str) -> Iterator[None]:
    """Capture noisy builder stdout/stderr so MCP stdio stays clean."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield
    output = buffer.getvalue().strip()
    if output:
        logger.info("%s output:\n%s", label, output)


def _tool_call(tool: str, func):
    try:
        return _json(func())
    except Exception as exc:
        logger.exception("%s failed", tool)
        return _error(
            tool,
            str(exc),
            traceback=traceback.format_exc().splitlines()[-20:],
        )


def _attach_existing_workflow(
    workflow_name: str,
    *,
    workspace: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    skills_root: Optional[str] = None,
) -> Any:
    workflow_name = _normalize_name(workflow_name, "workflow_name")
    workspace_dir = _resolve_workspace(workspace)
    root_dir = Path(workspace_dir) / workflow_name
    if not root_dir.exists():
        raise KeyError(
            f"workflow '{workflow_name}' is not registered and no directory exists at {root_dir}"
        )

    handle = registry.get_or_create(
        workspace=workspace_dir,
        workflow_name=workflow_name,
        api_key=api_key,
        model=model,
        provider=provider,
        skills_root=skills_root,
    )
    graph_path = root_dir / "graph_plan.json"
    workflow_json_path = root_dir / "workflow.json"
    requirement_path = root_dir / "requirement_analysis.md"
    main_path = root_dir / "main.py"

    chosen_graph = graph_path if graph_path.is_file() else workflow_json_path
    if not chosen_graph.is_file():
        raise KeyError(
            f"workflow '{workflow_name}' exists at {root_dir} but graph_plan.json/workflow.json is missing"
        )

    handle.graph_plan_path = str(chosen_graph)
    handle.builder.graph_plan_path = str(chosen_graph)
    if requirement_path.is_file():
        handle.requirement_md_path = str(requirement_path)
        handle.builder.requirement_md_path = str(requirement_path)
    if workflow_json_path.is_file():
        handle.workflow_json_path = str(workflow_json_path)
        handle.builder.workflow_json_path = str(workflow_json_path)
    if main_path.is_file():
        handle.main_entrypoint_path = str(main_path)

    with _capture_builder_output("attach_existing_workflow.load_graph"):
        handle.builder._load_planned_graph(handle.graph_plan_path)

    return handle


def _require_handle(workflow_name: str, workspace: Optional[str] = None) -> Any:
    workflow_name = _normalize_name(workflow_name, "workflow_name")
    workspace_dir = _resolve_workspace(workspace)
    handle = registry.get(workflow_name, workspace=workspace_dir)
    if handle is not None:
        return handle
    return _attach_existing_workflow(workflow_name, workspace=workspace_dir)


def _restart_builder_handle(
    workflow_name: str,
    *,
    workspace: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    skills_root: Optional[str] = None,
    reset_session: bool = True,
) -> tuple[Any, bool]:
    workflow = _normalize_name(workflow_name, "workflow_name")
    workspace_dir = _resolve_workspace(workspace)
    previous_handle = registry.pop(workflow, workspace=workspace_dir)

    backend_port: Optional[int] = None
    backend_was_running = False
    previous_session_id: Optional[str] = None
    previous_completed_steps: list[str] = []

    if previous_handle is not None:
        backend_port = previous_handle.backend_port
        if previous_handle.backend_port:
            backend_was_running = bool(
                previous_handle.is_running
                and health_check(previous_handle.backend_port, timeout=1.0)
            )
        previous_session_id = previous_handle.session_id
        previous_completed_steps = list(previous_handle.completed_steps)
        api_key = api_key or previous_handle.api_key or None
        model = model or previous_handle.model or None
        provider = provider or previous_handle.provider or None
        if skills_root is None:
            skills_root = previous_handle.skills_root
        previous_handle.stop_backend()

    handle = _attach_existing_workflow(
        workflow,
        workspace=workspace_dir,
        api_key=api_key,
        model=model,
        provider=provider,
        skills_root=skills_root,
    )
    if backend_port:
        handle.backend_port = backend_port
    if reset_session:
        handle.new_session()
    elif previous_session_id is not None:
        handle.session_id = previous_session_id
        handle.completed_steps = previous_completed_steps
    return handle, backend_was_running


def _load_steps_meta(handle: Any, *, include_hidden: bool = True) -> list[dict[str, Any]]:
    if not handle.graph_plan_path:
        raise ValueError(f"workflow '{handle.workflow_name}' does not have a graph plan path")
    handle.builder.graph_plan_path = handle.graph_plan_path
    with _capture_builder_output("load_steps_meta.load_graph"):
        handle.builder._load_planned_graph(handle.graph_plan_path)
    return handle.builder._build_steps_meta(include_hidden_nodes=include_hidden)


def _find_step_meta(handle: Any, step_id: str) -> Optional[dict[str, Any]]:
    target = str(step_id).strip()
    for step in _load_steps_meta(handle, include_hidden=True):
        if str(step.get("id", "")).strip() == target:
            return step
    return None


def _next_user_input_step(handle: Any) -> Optional[dict[str, Any]]:
    completed = set(handle.completed_steps)
    for step in _load_steps_meta(handle, include_hidden=True):
        step_id = str(step.get("id", "")).strip()
        if not step_id or step_id in completed:
            continue
        if not bool(step.get("inputRequired", False)):
            continue
        dependencies = step.get("dependencies") or []
        if all(str(dep).strip() in completed for dep in dependencies):
            return step
    return None


def _input_nodes_summary(handle: Any) -> list[dict[str, Any]]:
    with _capture_builder_output("get_node_input_formats"):
        formats = handle.builder.get_node_input_output_formats(
            graph_plan_path=handle.graph_plan_path,
            backend_language="python",
        )

    nodes: list[dict[str, Any]] = []
    for step in _load_steps_meta(handle, include_hidden=True):
        if not bool(step.get("inputRequired", False)):
            continue
        step_id = str(step.get("id", "")).strip()
        fmt = formats.get(step_id, {}) if isinstance(formats, dict) else {}
        ext_data = step.get("extData") if isinstance(step.get("extData"), dict) else {}
        nodes.append(
            {
                "stepId": step_id,
                "title": step.get("title", ""),
                "prompt": step.get("prompt", ""),
                "dependencies": list(step.get("dependencies") or []),
                "nodeKind": step.get("nodeKind", ""),
                "extData": ext_data,
                "user_input_format": fmt.get("user_input_format") or {},
                "backend_output_card_format": fmt.get("backend_output_card_format"),
                "backend_node_path": fmt.get("backend_node_path"),
            }
        )
    return nodes


def _parse_explicit_input_json(input_json: Any) -> Any:
    if input_json is None:
        return None
    if not isinstance(input_json, str):
        return input_json
    stripped = input_json.strip()
    if not stripped:
        return None

    should_parse = (
        stripped[0] in '{["'
        or stripped in {"true", "false", "null"}
        or bool(_JSON_NUMBER_RE.fullmatch(stripped))
    )
    if not should_parse:
        return input_json

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"input_json uses JSON syntax but could not be decoded: {exc.msg}"
        ) from exc


def _format_chat_request(
    *,
    chat_request: str,
    step_meta: Mapping[str, Any],
    user_input_format: Mapping[str, Any],
    file_path: Optional[str] = None,
    explicit_input: Any = None,
) -> tuple[Any, str]:
    if explicit_input is not None:
        return explicit_input, "used explicit input_json payload"

    node_kind = str(step_meta.get("nodeKind", "")).strip().lower()
    if node_kind == "file":
        chosen_path = str(file_path or chat_request or "").strip()
        if not chosen_path:
            raise ValueError(
                "file input step requires file_path or chat_request containing a filesystem path"
            )
        return {"file_path": str(Path(chosen_path).expanduser())}, "mapped file step to {'file_path': ...}"

    if isinstance(user_input_format, Mapping) and len(user_input_format) == 1:
        field_name = next(iter(user_input_format.keys()))
        if str(field_name).strip().lower() in {
            "text",
            "input",
            "value",
            "message",
            "prompt",
            "query",
            "request",
        }:
            return {field_name: chat_request}, f"mapped chat_request into single text-like field '{field_name}'"

    return chat_request, "passed chat_request as a plain string"


def _load_env_from_repo() -> None:
    load_env_from_runtime_context(__file__)


def create_server() -> Any:
    """Create and return the FlowX MCP server with all tools registered."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server could not load a compatible SDK entry point. "
            "Expected either 'mcp.server.fastmcp.FastMCP' or 'mcp.server.mcpserver.MCPServer'. "
            f"Install or repair the SDK with: {sys.executable} -m pip install -U 'mcp'. "
            f"Last import error: {_MCP_SERVER_IMPORT_ERROR}"
        )

    mcp = MCPServerClass(
        "flowx",
        instructions=(
            "FlowX workflow builder and runner. Use these tools to create AG-UI workflows "
            "with meta_agent, restart the in-memory builder, update node backends, start or reload the backend engine, "
            "inspect required user-input formats, upload or delete workspace files, and run "
            "workflow steps from chat input."
        ),
    )

    @mcp.tool()
    def create_workflow(
        workflow_name: str,
        user_prompt: str,
        workspace: Optional[str] = None,
        backend_port: int = 0,
        skills_root: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        """Create a workflow using meta_agent with a workspace and workflow name.

        Args:
            workflow_name: Logical name and folder name for the workflow.
            user_prompt: Natural-language requirement used to generate the workflow.
            workspace: Parent directory that will contain the workflow folder.
            backend_port: Optional backend port; 0 selects a free local port.
            skills_root: Optional skills root path passed to AgentBuilder.
            temperature: LLM temperature passed to the meta_agent generation calls.
        """

        def _impl() -> dict[str, Any]:
            if not meta_agent_available():
                raise ImportError(
                    "meta_agent is not importable. "
                    f"Underlying error: {meta_agent_import_error()}"
                )
            workflow = _normalize_name(workflow_name, "workflow_name")
            requirement = _normalize_name(user_prompt, "user_prompt")
            workspace_dir = _resolve_workspace(workspace)
            handle = registry.get_or_create(
                workspace=workspace_dir,
                workflow_name=workflow,
                skills_root=skills_root,
            )
            port = int(backend_port) if int(backend_port or 0) > 0 else find_free_port()
            handle.backend_port = port

            with _capture_builder_output("create_workflow.analyze_requirement"):
                req_path = handle.builder.analyze_requirement(requirement_text=requirement)
            with _capture_builder_output("create_workflow.plan_graph"):
                graph_path = handle.builder.plan_graph(
                    requirement_md_path=req_path,
                    graph_plan_filename="workflow.json",
                    temperature=temperature,
                )
            with _capture_builder_output("create_workflow.update_backend_nodes"):
                artifacts = handle.builder.update_backend_nodes(
                    graph_plan_path=graph_path,
                    requirement_md_path=req_path,
                    node_docs_dirname="node_docs",
                    language="python",
                    temperature=temperature,
                )
            with _capture_builder_output("create_workflow.sync_workflow_json"):
                workflow_json_path = handle.builder._sync_workflow_graph_json(
                    context_base_dir=handle.root_dir
                )
            with _capture_builder_output("create_workflow.generate_main"):
                main_entrypoint = handle.builder.generate_main_entrypoint(
                    graph_path,
                    output_filename="main.py",
                    temperature=temperature,
                    fastapi_port=port,
                )

            handle.sync_artifacts()
            input_nodes = _input_nodes_summary(handle)
            return {
                "ok": True,
                "workflow_name": handle.workflow_name,
                "workspace": handle.workspace,
                "root_dir": handle.root_dir,
                "backend_port": handle.backend_port,
                "session_id": handle.session_id,
                "requirement_md_path": handle.requirement_md_path,
                "graph_plan_path": handle.graph_plan_path,
                "workflow_json_path": workflow_json_path or handle.workflow_json_path,
                "main_entrypoint": str(main_entrypoint) or handle.main_entrypoint_path,
                "backend_nodes": artifacts.get("backend_nodes", {}),
                "input_nodes": input_nodes,
            }

        return _tool_call("create_workflow", _impl)

    @mcp.tool()
    def update_workflow_node(
        workflow_name: str,
        node_name: str,
        user_prompt: str,
        workspace: Optional[str] = None,
        backend_port: int = 0,
        temperature: float = 0.2,
    ) -> str:
        """Update one workflow node and regenerate workflow.json, the node backend, and main.py.

        Args:
            workflow_name: Existing workflow name.
            node_name: Node id/name to amend.
            user_prompt: Change request applied to the node and graph plan.
            workspace: Parent directory containing the workflow folder.
            backend_port: Optional backend port; if provided, main.py is regenerated for it.
            temperature: LLM temperature for the amendment calls.
        """

        def _impl() -> dict[str, Any]:
            handle = _require_handle(workflow_name, workspace=workspace)
            node = _normalize_name(node_name, "node_name")
            amendment = _normalize_name(user_prompt, "user_prompt")
            if int(backend_port or 0) > 0:
                handle.backend_port = int(backend_port)
            if not handle.backend_port:
                handle.backend_port = find_free_port()

            with _capture_builder_output("update_workflow_node.amend_workflow_json"):
                amended_workflow_json_path = handle.builder.amend_workflow_json(
                    user_prompt=amendment,
                    workflow_json_path=handle.workflow_json_path or handle.graph_plan_path or None,
                    temperature=temperature,
                )
            with _capture_builder_output("update_workflow_node.amend_node_markdown"):
                node_doc_path = handle.builder.amend_node_markdown(
                    node_name=node,
                    amendment=amendment,
                    requirement_md_path=handle.requirement_md_path or None,
                    graph_plan_path=amended_workflow_json_path or handle.graph_plan_path or None,
                    temperature=temperature,
                )
            with _capture_builder_output("update_workflow_node.generate_node"):
                regenerated_paths = handle.builder._generate_selected_nodes(
                    [node],
                    language="python",
                    temperature=temperature,
                    reset_mappings=False,
                )
            with _capture_builder_output("update_workflow_node.sync_workflow_json"):
                workflow_json_path = handle.builder._sync_workflow_graph_json(
                    context_base_dir=handle.root_dir
                )
            with _capture_builder_output("update_workflow_node.generate_main"):
                main_entrypoint = handle.builder.generate_main_entrypoint(
                    handle.builder.graph_plan_path,
                    output_filename="main.py",
                    temperature=temperature,
                    fastapi_port=handle.backend_port,
                )

            backend_was_running = bool(handle.is_running)
            handle.sync_artifacts()
            handle.new_session()
            if backend_was_running:
                handle.stop_backend()
            return {
                "ok": True,
                "workflow_name": handle.workflow_name,
                "node_name": node,
                "amended_workflow_json_path": amended_workflow_json_path,
                "node_doc_path": node_doc_path,
                "backend_node_paths": regenerated_paths,
                "workflow_json_path": workflow_json_path,
                "main_entrypoint": main_entrypoint,
                "session_id": handle.session_id,
                "completed_steps": list(handle.completed_steps),
                "backend_was_running": backend_was_running,
                "backend_running": bool(handle.is_running),
                "needs_reload": backend_was_running,
            }

        return _tool_call("update_workflow_node", _impl)

    @mcp.tool()
    def start_backend(
        workflow_name: str,
        workspace: Optional[str] = None,
        reset_session: bool = False,
        timeout_sec: int = 30,
    ) -> str:
        """Start the generated FastAPI backend engine for a workflow.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
            reset_session: If true, allocate a fresh session id before starting.
            timeout_sec: How long to wait for backend health.
        """

        def _impl() -> dict[str, Any]:
            handle = _require_handle(workflow_name, workspace=workspace)
            if not handle.backend_port:
                handle.backend_port = find_free_port()
            if reset_session:
                handle.new_session()

            if handle.is_running and health_check(handle.backend_port, timeout=1.0):
                return {
                    "ok": True,
                    "workflow_name": handle.workflow_name,
                    "already_running": True,
                    "session_id": handle.session_id,
                    "backend_port": handle.backend_port,
                    "base_url": handle.base_url(),
                    "main_entrypoint": handle.main_entrypoint_path,
                }

            with _capture_builder_output("start_backend.generate_main"):
                handle.main_entrypoint_path = str(
                    handle.builder.generate_main_entrypoint(
                        handle.graph_plan_path,
                        output_filename="main.py",
                        temperature=0.0,
                        fastapi_port=handle.backend_port,
                    )
                )
            with _capture_builder_output("start_backend.start"):
                info = handle.start_backend(
                    timeout=float(timeout_sec),
                )
            handle.sync_artifacts()
            info.update(
                {
                    "ok": bool(info.get("is_running", False)),
                    "session_id": handle.session_id,
                    "completed_steps": list(handle.completed_steps),
                }
            )
            return info

        return _tool_call("start_backend", _impl)

    @mcp.tool()
    def reload_workflow(
        workflow_name: str,
        workspace: Optional[str] = None,
        reset_session: bool = True,
        timeout_sec: int = 30,
    ) -> str:
        """Reload a workflow in the backend after files were updated.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
            reset_session: If true, allocate a new session id for the restarted backend.
            timeout_sec: How long to wait for backend health.
        """

        def _impl() -> dict[str, Any]:
            handle = _require_handle(workflow_name, workspace=workspace)
            if not handle.backend_port:
                handle.backend_port = find_free_port()

            with _capture_builder_output("reload_workflow.sync_workflow_json"):
                handle.workflow_json_path = handle.builder._sync_workflow_graph_json(
                    context_base_dir=handle.root_dir
                )
            with _capture_builder_output("reload_workflow.generate_main"):
                handle.main_entrypoint_path = str(
                    handle.builder.generate_main_entrypoint(
                        handle.graph_plan_path,
                        output_filename="main.py",
                        temperature=0.0,
                        fastapi_port=handle.backend_port,
                    )
                )
            with _capture_builder_output("reload_workflow.restart"):
                info = handle.reload_backend(
                    reset_session=reset_session,
                    timeout=float(timeout_sec),
                )

            handle.sync_artifacts()
            info.update(
                {
                    "ok": bool(info.get("is_running", False)),
                    "session_id": handle.session_id,
                    "completed_steps": list(handle.completed_steps),
                }
            )
            return info

        return _tool_call("reload_workflow", _impl)

    @mcp.tool()
    def restart_builder(
        workflow_name: str,
        workspace: Optional[str] = None,
        reset_session: bool = True,
        restart_backend: bool = False,
        timeout_sec: int = 30,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        skills_root: Optional[str] = None,
    ) -> str:
        """Recreate the in-memory AgentBuilder for a workflow from files on disk.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
            reset_session: If true, allocate a new session id for the recreated builder.
            restart_backend: If true, start the backend again after recreating the builder.
            timeout_sec: How long to wait for backend health when restarting it.
            api_key: Optional LLM API key override for the recreated builder.
            model: Optional LLM model override for the recreated builder.
            provider: Optional LLM provider override for the recreated builder.
            skills_root: Optional skills root path for the recreated builder.
        """

        def _impl() -> dict[str, Any]:
            handle, backend_was_running = _restart_builder_handle(
                workflow_name,
                workspace=workspace,
                api_key=api_key,
                model=model,
                provider=provider,
                skills_root=skills_root,
                reset_session=reset_session,
            )

            backend_info: Optional[dict[str, Any]] = None
            if restart_backend or backend_was_running:
                if not handle.backend_port:
                    handle.backend_port = find_free_port()
                with _capture_builder_output("restart_builder.generate_main"):
                    handle.main_entrypoint_path = str(
                        handle.builder.generate_main_entrypoint(
                            handle.graph_plan_path,
                            output_filename="main.py",
                            temperature=0.0,
                            fastapi_port=handle.backend_port,
                        )
                    )
                with _capture_builder_output("restart_builder.start"):
                    backend_info = handle.start_backend(
                        timeout=float(timeout_sec),
                    )

            handle.sync_artifacts()
            result: dict[str, Any] = {
                "ok": True,
                "workflow_name": handle.workflow_name,
                "workspace": handle.workspace,
                "root_dir": handle.root_dir,
                "session_id": handle.session_id,
                "backend_port": handle.backend_port,
                "builder_restarted": True,
                "backend_was_running": backend_was_running,
                "backend_restarted": backend_info is not None,
                "graph_plan_path": handle.graph_plan_path,
                "workflow_json_path": handle.workflow_json_path,
                "requirement_md_path": handle.requirement_md_path,
                "main_entrypoint": handle.main_entrypoint_path,
                "completed_steps": list(handle.completed_steps),
            }
            if backend_info is not None:
                result["backend"] = backend_info
                result["ok"] = bool(backend_info.get("is_running", False))
            return result

        return _tool_call("restart_builder", _impl)

    @mcp.tool()
    def get_node_input_formats(
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> str:
        """Get the expected input format for each workflow node that requires user input.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
        """

        def _impl() -> dict[str, Any]:
            handle = _require_handle(workflow_name, workspace=workspace)
            input_nodes = _input_nodes_summary(handle)
            next_step = _next_user_input_step(handle)
            return {
                "ok": True,
                "workflow_name": handle.workflow_name,
                "session_id": handle.session_id,
                "completed_steps": list(handle.completed_steps),
                "next_input_step": next_step,
                "input_nodes": input_nodes,
            }

        return _tool_call("get_node_input_formats", _impl)

    @mcp.tool()
    def run_workflow_step(
        workflow_name: str,
        chat_request: str = "",
        step_id: Optional[str] = None,
        workspace: Optional[str] = None,
        file_path: Optional[str] = None,
        input_json: Any = None,
        reset_session: bool = False,
        timeout_sec: int = DEFAULT_RUN_TIMEOUT,
    ) -> str:
        """Format a chat request into workflow input, run a step, and return the results.

        Args:
            workflow_name: Existing workflow name.
            chat_request: Natural-language request to send to the selected step.
            step_id: Optional explicit step id. If omitted, the next pending user-input step is used.
            workspace: Parent directory containing the workflow folder.
            file_path: Optional file path for file-input steps.
            input_json: Optional explicit payload to send as the step input. Structured JSON strings are decoded; plain strings are passed through unchanged.
            reset_session: If true, use a fresh workflow session.
            timeout_sec: How long to wait for the SSE run-step response.
        """

        def _impl() -> dict[str, Any]:
            handle = _require_handle(workflow_name, workspace=workspace)
            if reset_session:
                handle.new_session()

            if not handle.backend_port:
                handle.backend_port = find_free_port()
            if not handle.is_running or not health_check(handle.backend_port, timeout=1.0):
                with _capture_builder_output("run_workflow_step.autostart"):
                    start_info = handle.start_backend(timeout=30.0)
                if not start_info.get("is_running", False):
                    return {
                        "ok": False,
                        "workflow_name": handle.workflow_name,
                        "session_id": handle.session_id,
                        "error": start_info.get("error", "backend failed to start"),
                        "backend": start_info,
                    }

            selected_step = _find_step_meta(handle, step_id) if step_id else _next_user_input_step(handle)
            if selected_step is None:
                return {
                    "ok": True,
                    "workflow_name": handle.workflow_name,
                    "session_id": handle.session_id,
                    "message": "No pending user-input step was found.",
                    "completed_steps": list(handle.completed_steps),
                }

            requested_step_id = str(selected_step.get("id", "")).strip()
            input_nodes = {node["stepId"]: node for node in _input_nodes_summary(handle)}
            input_node = input_nodes.get(requested_step_id, {})
            explicit_input = _parse_explicit_input_json(input_json)
            formatted_input, formatting_note = _format_chat_request(
                chat_request=chat_request,
                step_meta=selected_step,
                user_input_format=input_node.get("user_input_format") or {},
                file_path=file_path,
                explicit_input=explicit_input,
            )

            response = run_step_sse(
                handle.base_url(),
                session_id=handle.session_id,
                step_id=requested_step_id,
                input=formatted_input,
                file_path=file_path,
                timeout=float(timeout_sec),
            )
            completed_steps = response.get("completedSteps") or []
            if isinstance(completed_steps, list):
                handle.completed_steps = [str(item) for item in completed_steps]

            next_step = _next_user_input_step(handle)
            return {
                "ok": bool(response.get("ok", False)),
                "workflow_name": handle.workflow_name,
                "session_id": handle.session_id,
                "selected_step": {
                    "stepId": requested_step_id,
                    "title": selected_step.get("title", ""),
                    "prompt": selected_step.get("prompt", ""),
                    "dependencies": list(selected_step.get("dependencies") or []),
                    "nodeKind": selected_step.get("nodeKind", ""),
                },
                "formatted_input": formatted_input,
                "formatting_note": formatting_note,
                "response": response,
                "next_input_step": next_step,
            }

        return _tool_call("run_workflow_step", _impl)

    @mcp.tool()
    def list_workflows() -> str:
        """List workflows known to the current MCP server process."""

        def _impl() -> dict[str, Any]:
            return {
                "ok": True,
                "workflows": registry.list_workflows(),
            }

        return _tool_call("list_workflows", _impl)

    @mcp.tool()
    def kill_workflow(
        workflow_name: str,
        backend_port: int,
        workspace: Optional[str] = None,
    ) -> str:
        """Stop and unregister a workflow by workflow name and backend port.

        Args:
            workflow_name: Existing workflow name.
            backend_port: Backend port that must match the registered workflow.
            workspace: Parent directory containing the workflow folder.
        """

        def _impl() -> dict[str, Any]:
            workflow = _normalize_name(workflow_name, "workflow_name")
            port = int(backend_port)
            if port <= 0:
                raise ValueError("backend_port must be a positive integer")

            workspace_dir = _resolve_workspace(workspace)
            existing = registry.get(workflow, workspace=workspace_dir)
            was_running = bool(existing and existing.is_running)
            stopped = registry.kill_workflow(
                workflow,
                backend_port=port,
                workspace=workspace_dir,
            )
            return {
                "ok": True,
                "workflow_name": stopped.workflow_name,
                "workspace": stopped.workspace,
                "root_dir": stopped.root_dir,
                "backend_port": port,
                "was_running": was_running,
                "is_running": stopped.is_running,
                "removed_from_registry": True,
            }

        return _tool_call("kill_workflow", _impl)

    @mcp.tool()
    def list_workflow_folders(workspace: Optional[str] = None) -> str:
        """List workflow folder names under the workspace root on disk.

        Args:
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workspace_dir = _resolve_workspace(workspace)
            workflows = _discover_workflow_folders(workspace_dir)
            return {
                "ok": True,
                "workspace": workspace_dir,
                "count": len(workflows),
                "workflow_names": [item["workflow_name"] for item in workflows],
                "workflows": workflows,
            }

        return _tool_call("list_workflow_folders", _impl)

    @mcp.tool()
    def upload_workspace_input_file(
        file_name: str,
        content_base64: str,
        workspace: Optional[str] = None,
        overwrite: bool = False,
    ) -> str:
        """Upload a base64-encoded file into workspace/inputs.

        Args:
            file_name: File name to write under workspace/inputs, including any extension.
            content_base64: Base64-encoded file bytes or a data URL.
            workspace: Workspace root that owns the inputs folder.
            overwrite: If true, replace an existing file with the same name.
        """

        def _impl() -> dict[str, Any]:
            saved_file = _write_workspace_input_file(
                file_name=file_name,
                content_base64=content_base64,
                workspace=workspace,
                overwrite=overwrite,
            )
            return {
                "ok": True,
                **saved_file,
            }

        return _tool_call("upload_workspace_input_file", _impl)

    @mcp.tool()
    def delete_workspace_files(
        file_names: list[str],
        workspace: Optional[str] = None,
    ) -> str:
        """Delete specific files under a workspace by file name or relative path.

        Args:
            file_names: File names or unique relative paths under the workspace root.
            workspace: Workspace root that owns the files.
        """

        def _impl() -> dict[str, Any]:
            workspace_dir = _resolve_workspace(workspace)
            deleted_files = _delete_workspace_files_by_name(
                list(file_names),
                workspace=workspace_dir,
            )
            return {
                "ok": True,
                "workspace": workspace_dir,
                "count": len(deleted_files),
                "deleted_files": deleted_files,
            }

        return _tool_call("delete_workspace_files", _impl)

    @mcp.tool()
    def list_workflow_python_files(
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> str:
        """List all Python files under a workflow folder.

        Args:
            workflow_name: Existing workflow folder name.
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workflow_root = _resolve_workflow_root(workflow_name, workspace=workspace)
            files = _list_python_workflow_files(workflow_root)
            return {
                "ok": True,
                "workflow_name": workflow_root.name,
                "workspace": str(workflow_root.parent),
                "count": len(files),
                "file_names": [item["file_name"] for item in files],
                "files": files,
            }

        return _tool_call("list_workflow_python_files", _impl)

    @mcp.tool()
    def get_workflow_json(
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> str:
        """Read the root workflow.json file for a workflow folder.

        Args:
            workflow_name: Existing workflow folder name.
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workflow_root = _resolve_workflow_root(workflow_name, workspace=workspace)
            workflow_file = _get_workflow_files_by_name(workflow_root, ["workflow.json"])[0]
            return {
                "ok": True,
                "workflow_name": workflow_root.name,
                "workspace": str(workflow_root.parent),
                "file_name": workflow_file["file_name"],
                "relative_path": workflow_file["relative_path"],
                "workflow_json": json.loads(workflow_file["content"]),
            }

        return _tool_call("get_workflow_json", _impl)

    @mcp.tool()
    def get_workflow_files(
        workflow_name: str,
        file_names: list[str],
        workspace: Optional[str] = None,
    ) -> str:
        """Read specific files from a workflow folder by file name or relative path.

        Args:
            workflow_name: Existing workflow folder name.
            file_names: File names or unique relative paths under the workflow folder.
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workflow_root = _resolve_workflow_root(workflow_name, workspace=workspace)
            files = _get_workflow_files_by_name(workflow_root, list(file_names))
            return {
                "ok": True,
                "workflow_name": workflow_root.name,
                "workspace": str(workflow_root.parent),
                "count": len(files),
                "files": files,
            }

        return _tool_call("get_workflow_files", _impl)

    @mcp.tool()
    def get_workflow_binary_files(
        workflow_name: str,
        file_names: list[str],
        workspace: Optional[str] = None,
    ) -> str:
        """Read specific binary files from a workflow folder by file name or relative path.

        Args:
            workflow_name: Existing workflow folder name.
            file_names: File names or unique relative paths under the workflow folder.
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workflow_root = _resolve_workflow_root(workflow_name, workspace=workspace)
            files = _get_workflow_binary_files_by_name(workflow_root, list(file_names))
            return {
                "ok": True,
                "workflow_name": workflow_root.name,
                "workspace": str(workflow_root.parent),
                "count": len(files),
                "files": files,
            }

        return _tool_call("get_workflow_binary_files", _impl)

    @mcp.tool()
    def replace_workflow_files(
        workflow_name: str,
        file_names: list[str],
        new_file_contents: list[str],
        workspace: Optional[str] = None,
    ) -> str:
        """Replace specific files in a workflow folder.

        Args:
            workflow_name: Existing workflow folder name.
            file_names: File names or unique relative paths under the workflow folder.
            new_file_contents: Replacement text for each requested file.
            workspace: Parent directory containing workflow folders.
        """

        def _impl() -> dict[str, Any]:
            workflow_root = _resolve_workflow_root(workflow_name, workspace=workspace)
            updated_files = _replace_workflow_files_by_name(
                workflow_root,
                list(file_names),
                list(new_file_contents),
            )
            return {
                "ok": True,
                "workflow_name": workflow_root.name,
                "workspace": str(workflow_root.parent),
                "count": len(updated_files),
                "updated_files": updated_files,
            }

        return _tool_call("replace_workflow_files", _impl)

    return mcp


def run_stdio_server(verbose: bool = False) -> None:
    """Start the FlowX MCP server on stdio."""
    run_server(verbose=verbose)


def run_server(
    *,
    verbose: bool = False,
) -> None:
    """Start the local FlowX MCP server on stdio."""
    if not _MCP_SERVER_AVAILABLE:
        print(
            "Error: MCP server could not load a compatible MCP SDK entry point.\n"
            "Expected either mcp.server.fastmcp.FastMCP or mcp.server.mcpserver.MCPServer.\n"
            f"Install or repair with: {sys.executable} -m pip install -U 'mcp'\n"
            f"Last import error: {_MCP_SERVER_IMPORT_ERROR}",
            file=sys.stderr,
        )
        sys.exit(1)

    _load_env_from_repo()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
    )
    server = create_server()

    try:
        run = getattr(server, "run", None)
        if callable(run):
            run()
            return

        run_stdio_async = getattr(server, "run_stdio_async", None)
        if not callable(run_stdio_async):
            raise RuntimeError(
                "The installed MCP SDK does not expose a compatible server runner. "
                "Expected server.run(...) or server.run_stdio_async()."
            )

        async def _run_stdio() -> None:
            await run_stdio_async()

        asyncio.run(_run_stdio())
    except KeyboardInterrupt:
        logger.info("FlowX MCP server interrupted")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run FlowX as a local stdio MCP server")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging on stderr",
    )
    args = parser.parse_args(argv)
    run_server(verbose=bool(args.verbose))


if __name__ == "__main__":  # pragma: no cover
    main()
