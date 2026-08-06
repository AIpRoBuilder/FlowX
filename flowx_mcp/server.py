"""FastMCP server that wraps ``meta_agent`` and ``ag_ui_workflow`` workflows."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import importlib
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

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

load_dotenv: Any = None
try:
    load_dotenv = importlib.import_module("dotenv").load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
    )
    graph_path = root_dir / "graph_plan.json"
    workflow_json_path = root_dir / "workflow.json"
    requirement_path = root_dir / "requirement_analysis.md"
    main_path = root_dir / "main.py"
    frontend_path = root_dir / "frontend" / "src"

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
    if frontend_path.exists():
        handle.frontend_output_path = str(frontend_path)

    with _capture_builder_output("attach_existing_workflow.load_graph"):
        handle.builder._load_planned_graph(handle.graph_plan_path)

    return handle


def _require_handle(workflow_name: str, workspace: Optional[str] = None) -> Any:
    workflow_name = _normalize_name(workflow_name, "workflow_name")
    handle = registry.get(workflow_name)
    if handle is not None:
        return handle
    return _attach_existing_workflow(workflow_name, workspace=workspace)


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


def _parse_explicit_input_json(input_json: Optional[str]) -> Any:
    if input_json is None:
        return None
    stripped = input_json.strip()
    if not stripped:
        return None
    return json.loads(stripped)


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
    if load_dotenv is None:
        return
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


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
            "with meta_agent, update node backends, start or reload the backend engine, "
            "inspect required user-input formats, and run workflow steps from chat input."
        ),
    )

    @mcp.tool()
    def create_workflow(
        workflow_name: str,
        user_prompt: str,
        workspace: Optional[str] = None,
        backend_port: int = 0,
        services_root: Optional[str] = None,
        skills_root: Optional[str] = None,
        frontend_style_prompt: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        """Create a workflow using meta_agent with a workspace and workflow name.

        Args:
            workflow_name: Logical name and folder name for the workflow.
            user_prompt: Natural-language requirement used to generate the workflow.
            workspace: Parent directory that will contain the workflow folder.
            backend_port: Optional backend port; 0 selects a free local port.
            services_root: Optional services root path passed to AgentBuilder.
            skills_root: Optional skills root path passed to AgentBuilder.
            frontend_style_prompt: Optional styling prompt for the generated frontend.
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
                services_root=services_root,
                skills_root=skills_root,
            )
            port = int(backend_port) if int(backend_port or 0) > 0 else find_free_port()
            handle.backend_port = port
            if isinstance(frontend_style_prompt, str):
                handle.builder.frontend_style_prompt = frontend_style_prompt.strip() or None

            with _capture_builder_output("create_workflow.analyze_requirement"):
                req_path = handle.builder.analyze_requirement(requirement_text=requirement)
            with _capture_builder_output("create_workflow.plan_graph"):
                graph_path = handle.builder.plan_graph(
                    requirement_md_path=req_path,
                    graph_plan_filename="workflow.json",
                    temperature=temperature,
                    services_root=services_root,
                )
            with _capture_builder_output("create_workflow.update_backend_nodes"):
                artifacts = handle.builder.update_backend_nodes(
                    graph_plan_path=graph_path,
                    requirement_md_path=req_path,
                    node_docs_dirname="node_docs",
                    node_ui_dirname="node_ui",
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

            handle.sync_artifacts()
            return {
                "ok": True,
                "workflow_name": handle.workflow_name,
                "node_name": node,
                "amended_workflow_json_path": amended_workflow_json_path,
                "node_doc_path": node_doc_path,
                "backend_node_paths": regenerated_paths,
                "workflow_json_path": workflow_json_path,
                "main_entrypoint": main_entrypoint,
                "backend_running": bool(handle.is_running),
                "needs_reload": bool(handle.is_running),
            }

        return _tool_call("update_workflow_node", _impl)

    @mcp.tool()
    def start_backend(
        workflow_name: str,
        workspace: Optional[str] = None,
        reset_session: bool = False,
        with_frontend: bool = False,
        timeout_sec: int = 30,
    ) -> str:
        """Start the generated FastAPI backend engine for a workflow.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
            reset_session: If true, allocate a fresh session id before starting.
            with_frontend: Also start the generated Vue dev server via AgentBuilder.rerun_server().
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
                    with_frontend=with_frontend,
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
        with_frontend: bool = False,
        timeout_sec: int = 30,
    ) -> str:
        """Reload a workflow in the backend after files were updated.

        Args:
            workflow_name: Existing workflow name.
            workspace: Parent directory containing the workflow folder.
            reset_session: If true, allocate a new session id for the restarted backend.
            with_frontend: Also restart the generated Vue dev server.
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
                    with_frontend=with_frontend,
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
        input_json: Optional[str] = None,
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
            input_json: Optional explicit JSON payload to send as the step input.
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
                    start_info = handle.start_backend(with_frontend=False, timeout=30.0)
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

    return mcp


def run_stdio_server(verbose: bool = False) -> None:
    """Start the FlowX MCP server on stdio."""
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

    async def _run() -> None:
        await server.run_stdio_async()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("FlowX MCP server interrupted")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run FlowX as an MCP server")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging on stderr",
    )
    args = parser.parse_args(argv)
    run_stdio_server(verbose=bool(args.verbose))


if __name__ == "__main__":  # pragma: no cover
    main()
