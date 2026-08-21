"""Per-workflow state registry and backend process management.

A :class:`WorkflowHandle` bundles one ``meta_agent.AgentBuilder`` instance together with
the runtime state of the workflow it owns: the generated artifact paths, the running
backend process, the HTTP port it listens on and the current session/progress state.

The :class:`WorkflowRegistry` is a process-wide singleton (the MCP server is a single
long-running stdio process) that maps a workflow name to its handle so that successive
tool calls operate on the same ``AgentBuilder`` (preserving its ``dynamic_graph_cache``,
``node_coder_map`` etc.).
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("flowx.registry")

# --- lazy meta_agent import -------------------------------------------------
try:
    from meta_agent.agent_builder import AgentBuilder  # type: ignore
    _META_AGENT_AVAILABLE = True
    _IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - import guard
    AgentBuilder = None  # type: ignore[assignment]
    _META_AGENT_AVAILABLE = False
    _IMPORT_ERROR = str(exc)


def meta_agent_available() -> bool:
    return _META_AGENT_AVAILABLE


def meta_agent_import_error() -> Optional[str]:
    return _IMPORT_ERROR


# --- helpers ----------------------------------------------------------------
def _select_python_command() -> str:
    """Pick a python executable, preferring the one meta_agent would use."""
    try:
        from meta_agent.tools.agent_builder_tools import select_python_command  # type: ignore
        return select_python_command()
    except Exception:
        return "python3"


def find_free_port() -> int:
    """Return a free TCP port on localhost (best-effort)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _default_llm_config() -> Dict[str, str]:
    return {
        "provider": os.environ.get("FLOWX_LLM_PROVIDER", "deepseek"),
        "model": os.environ.get("FLOWX_LLM_MODEL", "deepseek-chat"),
        "api_key": (
            os.environ.get("FLOWX_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        ),
        "base_url": os.environ.get("FLOWX_LLM_BASE_URL", ""),
    }


def _registry_key(workspace: str, workflow_name: str) -> Tuple[str, str]:
    return (str(Path(workspace).expanduser().resolve()), workflow_name)


# --- handle -----------------------------------------------------------------
@dataclass
class WorkflowHandle:
    """All state the MCP server keeps for one workflow."""

    workflow_name: str
    workspace: str
    root_dir: str
    builder: Any  # meta_agent.AgentBuilder
    api_key: str
    model: str
    provider: str
    services_root: Optional[str] = None
    skills_root: Optional[str] = None
    frontend_style_prompt: Optional[str] = None

    # generated artifacts
    graph_plan_path: str = ""
    requirement_md_path: str = ""
    workflow_json_path: str = ""
    main_entrypoint_path: str = ""
    frontend_output_path: str = ""

    # runtime
    backend_port: Optional[int] = None
    backend_process: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
    backend_log_path: str = ""
    is_running: bool = False

    # session / progress
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    completed_steps: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ utils
    def base_url(self) -> str:
        if not self.backend_port:
            raise RuntimeError("backend port is not assigned")
        return f"http://127.0.0.1:{self.backend_port}"

    def new_session(self) -> str:
        self.session_id = uuid.uuid4().hex[:12]
        self.completed_steps = []
        return self.session_id

    # ----------------------------------------------------------- backend proc
    def stop_backend(self) -> None:
        """Stop the backend (and any frontend) managed by this handle."""
        # Processes started by AgentBuilder.rerun_server()
        try:
            if self.builder is not None:
                self.builder._stop_managed_server_process(
                    getattr(self.builder, "backend_server_process", None)
                )
                self.builder._stop_managed_server_process(
                    getattr(self.builder, "frontend_server_process", None)
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("stop builder-managed processes failed: %s", exc)

        proc = self.backend_process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("terminate backend failed: %s", exc)
        self.backend_process = None
        self.is_running = False

    def _ensure_main_entrypoint(self) -> str:
        """Make sure main.py exists (regenerate with the assigned port if missing)."""
        if self.main_entrypoint_path and Path(self.main_entrypoint_path).is_file():
            return self.main_entrypoint_path
        port = self.backend_port or 8000
        path = self.builder.generate_main_entrypoint(
            self.graph_plan_path or self.builder.graph_plan_path,
            output_filename="main.py",
            temperature=0.0,
            fastapi_port=port,
        )
        self.main_entrypoint_path = str(path)
        return self.main_entrypoint_path

    def start_backend(
        self,
        *,
        with_frontend: bool = False,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Start the generated FastAPI backend.

        ``with_frontend=True`` delegates to ``AgentBuilder.rerun_server`` (which also
        launches the Vue dev server via npm and therefore requires a generated
        frontend project). Otherwise a backend-only ``python main.py`` subprocess is
        launched — the common case for headless workflow execution.
        """
        from .client import health_check

        self.stop_backend()
        self._ensure_main_entrypoint()
        port = self.backend_port or 8000
        self.backend_port = port

        if with_frontend:
            runtime = self.builder.rerun_server(
                graph_plan_path=self.graph_plan_path,
                backend_port=port,
            )
            self.backend_process = getattr(self.builder, "backend_server_process", None)
            self.is_running = True
        else:
            python_cmd = _select_python_command()
            log_path = str(Path(self.root_dir) / "backend.log")
            self.backend_log_path = log_path
            log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for process lifetime
            log_fh.write(f"\n=== flowx backend start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            log_fh.flush()
            self.backend_process = subprocess.Popen(  # type: ignore[assignment]
                [python_cmd, str(self.main_entrypoint_path)],
                cwd=str(Path(self.main_entrypoint_path).parent),
                env=os.environ.copy(),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
            self.is_running = True

        ready = health_check(port, timeout=timeout)
        info: Dict[str, Any] = {
            "workflow_name": self.workflow_name,
            "port": port,
            "pid": getattr(self.backend_process, "pid", None) if self.backend_process else None,
            "is_running": self.is_running and ready,
            "base_url": self.base_url(),
            "main_entrypoint": self.main_entrypoint_path,
        }
        if not ready:
            info["is_running"] = False
            info["error"] = "backend did not become healthy within timeout"
            info["log_path"] = self.backend_log_path
            # surface the tail of the log so the caller can see import errors etc.
            info["log_tail"] = _read_log_tail(self.backend_log_path, 40)
        return info

    def reload_backend(
        self,
        *,
        reset_session: bool = True,
        with_frontend: bool = False,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Restart the backend so updated node files / workflow.json take effect."""
        self.stop_backend()
        if reset_session:
            self.new_session()
        return self.start_backend(with_frontend=with_frontend, timeout=timeout)

    # --------------------------------------------------------------- artifacts
    def sync_artifacts(self) -> None:
        """Refresh the cached artifact paths from the builder."""
        self.graph_plan_path = str(getattr(self.builder, "graph_plan_path", "") or "")
        self.requirement_md_path = str(getattr(self.builder, "requirement_md_path", "") or "")
        self.workflow_json_path = str(getattr(self.builder, "workflow_json_path", "") or "")
        self.frontend_output_path = str(getattr(self.builder, "frontend_output_path", "") or "")
        main_path = getattr(self.builder, "main_output_path", None) or getattr(self.builder, "main_entrypoint_path", None)
        if main_path:
            self.main_entrypoint_path = str(main_path)


def _read_log_tail(path: str, lines: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except Exception:
        return ""


# --- registry ---------------------------------------------------------------
class WorkflowRegistry:
    """Process-wide registry of workflow handles keyed by workspace and workflow name."""

    def __init__(self) -> None:
        self._handles: Dict[Tuple[str, str], WorkflowHandle] = {}
        self._lock = threading.RLock()

    def _find_unlocked(
        self,
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> Optional[WorkflowHandle]:
        if workspace is not None:
            return self._handles.get(_registry_key(workspace, workflow_name))

        matches = [
            handle
            for (handle_workspace, handle_name), handle in self._handles.items()
            if handle_name == workflow_name
        ]
        if len(matches) > 1:
            raise KeyError(
                f"workflow '{workflow_name}' exists in multiple workspaces; pass workspace to disambiguate"
            )
        if not matches:
            return None
        return matches[0]

    def list_workflows(self, workspace: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            resolved_workspace = (
                str(Path(workspace).expanduser().resolve()) if workspace is not None else None
            )
            return [
                {
                    "workflow_name": h.workflow_name,
                    "workspace": h.workspace,
                    "root_dir": h.root_dir,
                    "graph_plan_path": h.graph_plan_path,
                    "main_entrypoint_path": h.main_entrypoint_path,
                    "backend_port": h.backend_port,
                    "is_running": h.is_running,
                    "session_id": h.session_id,
                    "completed_steps": list(h.completed_steps),
                }
                for h in self._handles.values()
                if resolved_workspace is None or h.workspace == resolved_workspace
            ]

    def get(
        self,
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> Optional[WorkflowHandle]:
        with self._lock:
            return self._find_unlocked(workflow_name, workspace)

    def require(self, workflow_name: str, workspace: Optional[str] = None) -> WorkflowHandle:
        handle = self.get(workflow_name, workspace=workspace)
        if handle is None:
            raise KeyError(
                f"workflow '{workflow_name}' is not registered. "
                "Call create_workflow first (with the same workflow_name)."
            )
        return handle

    def pop(
        self,
        workflow_name: str,
        workspace: Optional[str] = None,
    ) -> Optional[WorkflowHandle]:
        with self._lock:
            handle = self._find_unlocked(workflow_name, workspace)
            if handle is None:
                return None
            return self._handles.pop(_registry_key(handle.workspace, handle.workflow_name), None)

    def find_by_backend_port(self, backend_port: int) -> Optional[WorkflowHandle]:
        with self._lock:
            for handle in self._handles.values():
                if handle.backend_port == backend_port:
                    return handle
        return None

    def kill_workflow(
        self,
        workflow_name: str,
        *,
        backend_port: int,
        workspace: Optional[str] = None,
    ) -> WorkflowHandle:
        requested_port = int(backend_port)
        if requested_port <= 0:
            raise ValueError("backend_port must be a positive integer")

        with self._lock:
            handle = self._find_unlocked(workflow_name, workspace)
            if handle is None:
                port_owner = next(
                    (item for item in self._handles.values() if item.backend_port == requested_port),
                    None,
                )
                if port_owner is not None:
                    raise KeyError(
                        f"workflow '{workflow_name}' is not registered; "
                        f"backend_port {requested_port} belongs to workflow '{port_owner.workflow_name}'"
                    )
                raise KeyError(f"workflow '{workflow_name}' is not registered")

            assigned_port = handle.backend_port
            if assigned_port is None:
                raise ValueError(
                    f"workflow '{workflow_name}' does not have a backend_port assigned"
                )
            if assigned_port != requested_port:
                port_owner = next(
                    (item for item in self._handles.values() if item.backend_port == requested_port),
                    None,
                )
                if port_owner is not None and port_owner.workflow_name != workflow_name:
                    raise ValueError(
                        f"workflow '{workflow_name}' is registered on backend_port {assigned_port}; "
                        f"backend_port {requested_port} belongs to workflow '{port_owner.workflow_name}'"
                    )
                raise ValueError(
                    f"workflow '{workflow_name}' is registered on backend_port {assigned_port}, "
                    f"not {requested_port}"
                )

            stopped_handle = self._handles.pop(
                _registry_key(handle.workspace, handle.workflow_name)
            )

        stopped_handle.stop_backend()
        return stopped_handle

    def get_or_create(
        self,
        *,
        workspace: str,
        workflow_name: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        services_root: Optional[str] = None,
        skills_root: Optional[str] = None,
    ) -> WorkflowHandle:
        if not _META_AGENT_AVAILABLE:
            raise ImportError(
                "meta_agent is not importable. Install it (pip install -e ../meta_agent) "
                f"or set FLOWX_EXTRA_PATHS. Underlying error: {_IMPORT_ERROR}"
            )
        with self._lock:
            resolved_workspace = str(Path(workspace).expanduser().resolve())
            key = _registry_key(resolved_workspace, workflow_name)
            existing = self._handles.get(key)
            if existing is not None:
                # Reuse the long-lived builder; allow callers to refresh credentials.
                if api_key or model or provider:
                    existing.builder.reset_llm_config(
                        api_key=api_key or None,
                        model=model or None,
                        provider=provider or None,
                    )
                    if api_key:
                        existing.api_key = api_key
                    if model:
                        existing.model = model
                    if provider:
                        existing.provider = provider
                if services_root is not None:
                    existing.services_root = services_root
                if skills_root is not None:
                    existing.skills_root = skills_root
                return existing

            defaults = _default_llm_config()
            eff_api_key = api_key or defaults["api_key"]
            eff_model = model or defaults["model"]
            eff_provider = provider or defaults["provider"]
            root_dir = str(Path(resolved_workspace) / workflow_name)
            Path(root_dir).mkdir(parents=True, exist_ok=True)

            builder = AgentBuilder(  # type: ignore[misc]
                api_key=eff_api_key,
                model=eff_model,
                provider=eff_provider,
                root_dir=root_dir,
                services_root_path=services_root,
                skills_root_path=skills_root,
            )
            handle = WorkflowHandle(
                workflow_name=workflow_name,
                workspace=resolved_workspace,
                root_dir=root_dir,
                builder=builder,
                api_key=eff_api_key,
                model=eff_model,
                provider=eff_provider,
                services_root=services_root,
                skills_root=skills_root,
            )
            self._handles[key] = handle
            return handle


# process-wide singleton
registry = WorkflowRegistry()
