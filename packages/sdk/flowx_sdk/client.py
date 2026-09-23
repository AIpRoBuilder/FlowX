"""Python SDK for compiling and evolving FlowX workflows."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from flowx_core.agent_builder import AgentBuilder


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class WorkflowArtifacts:
    """Paths generated while compiling one workflow."""

    workflow_name: str
    root_dir: Path
    requirement_md_path: Path
    workflow_json_path: Path
    main_entrypoint_path: Path
    backend_port: int


class FlowXClient:
    """Create and update workflows without an MCP transport.

    The client owns one :class:`AgentBuilder` per workflow, preserving the
    builder's graph and node-generation caches for subsequent updates.
    """

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        configured_workspace = workspace or os.environ.get("FLOWX_DEFAULT_WORKSPACE")
        self.workspace = Path(configured_workspace or ".flowx_workspaces").expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.environ.get("FLOWX_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self.model = model or os.environ.get("FLOWX_LLM_MODEL", "deepseek-chat")
        self.provider = provider or os.environ.get("FLOWX_LLM_PROVIDER", "deepseek")
        self._builders: dict[str, AgentBuilder] = {}

    def builder(self, workflow_name: str, *, skills_root: str | None = None) -> AgentBuilder:
        """Return the cached builder for a workflow, creating it when needed."""
        name = _normalize_name(workflow_name, "workflow_name")
        builder = self._builders.get(name)
        if builder is None:
            builder = AgentBuilder(
                api_key=self.api_key,
                model=self.model,
                provider=self.provider,
                root_dir=str(self.workspace / name),
                skills_root_path=skills_root,
            )
            self._builders[name] = builder
        return builder

    def create_workflow(
        self,
        workflow_name: str,
        requirement: str,
        *,
        backend_port: int = 0,
        skills_root: str | None = None,
        temperature: float = 0.3,
    ) -> WorkflowArtifacts:
        """Compile a requirement into an executable FlowX workflow."""
        name = _normalize_name(workflow_name, "workflow_name")
        prompt = str(requirement).strip()
        if not prompt:
            raise ValueError("requirement must be a non-empty string")

        builder = self.builder(name, skills_root=skills_root)
        port = int(backend_port) if backend_port > 0 else _find_free_port()
        requirement_path = Path(builder.analyze_requirement(requirement_text=prompt))
        graph_path = Path(
            builder.plan_graph(
                requirement_md_path=str(requirement_path),
                graph_plan_filename="workflow.json",
                temperature=temperature,
            )
        )
        builder.update_backend_nodes(
            graph_plan_path=str(graph_path),
            requirement_md_path=str(requirement_path),
            node_docs_dirname="node_docs",
            language="python",
            temperature=temperature,
        )
        workflow_json_path = Path(builder._sync_workflow_graph_json(context_base_dir=builder.root_dir))
        main_path = Path(
            builder.generate_main_entrypoint(
                str(graph_path),
                output_filename="main.py",
                temperature=temperature,
                fastapi_port=port,
            )
        )
        return WorkflowArtifacts(
            workflow_name=name,
            root_dir=Path(builder.root_dir).resolve(),
            requirement_md_path=requirement_path.resolve(),
            workflow_json_path=workflow_json_path.resolve(),
            main_entrypoint_path=main_path.resolve(),
            backend_port=port,
        )

    def update_workflow_node(
        self,
        workflow_name: str,
        node_name: str,
        amendment: str,
        *,
        temperature: float = 0.2,
    ) -> Path:
        """Apply an amendment to one existing workflow node."""
        builder = self.builder(workflow_name)
        node = _normalize_name(node_name, "node_name")
        prompt = str(amendment).strip()
        if not prompt:
            raise ValueError("amendment must be a non-empty string")
        updated_path = builder.amend_workflow_json(
            user_prompt=prompt,
            workflow_json_path=builder.workflow_json_path or builder.graph_plan_path or None,
            temperature=temperature,
        )
        builder.amend_node_markdown(
            node_name=node,
            amendment=prompt,
            requirement_md_path=builder.requirement_md_path or None,
            graph_plan_path=updated_path or builder.graph_plan_path or None,
            temperature=temperature,
        )
        builder._generate_selected_nodes(
            [node],
            language="python",
            temperature=temperature,
            reset_mappings=False,
        )
        return Path(builder._sync_workflow_graph_json(context_base_dir=builder.root_dir)).resolve()


def _normalize_name(value: str, label: str) -> str:
    name = str(value).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"{label} must be a non-empty plain directory name")
    return name
