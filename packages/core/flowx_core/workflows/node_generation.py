"""Dependency-aware workflow for generating and amending graph nodes.

Modified: construct exact concrete classes with instance-only scoped settings.
Modified: reuse an eager workflow across request-local node kinds without retaining results.
Modified 2026-10-15: merge batch context and per-node inputs into one request model.
Modified 2026-10-15: derive node identity from metadata and keep routing data in the builder.
Modified 2026-10-15: accept one complete node request without batch orchestration.
Modified 2026-10-15: defer batch orchestration and allow explicit per-request planner skipping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from ag_ui_workflow import WorkflowEngine, WorkflowStepNode

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.architect.graph import NodeMeta
from flowx_core.tools._node_settings import node_settings
from flowx_core.tools.file_tools import parse_json_file
from flowx_core.tools.workflow_engine import (
    close_workflow_engine,
    init_workflow_engine,
    run_workflow,
)
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


@dataclass(frozen=True, kw_only=True)
class NodeGenerationRequest:
    """Inputs for one node; explicit planner skipping overrides markdown options."""

    graph_plan_path: str
    requirement_md_path: str
    requirement_text: str
    node_meta: NodeMeta
    raw_node: Mapping[str, Any]
    target_path: Path
    markdown_path: Path
    language: str = "python"
    temperature: float = 0.35
    generate_markdown: bool = True
    skip_node_planner: bool = False
    markdown_amendment: str = ""
    node_amendment: str = ""
    node_format: Mapping[str, Any] = field(default_factory=dict)
    dependency_context: str = ""
    state: Mapping[str, Any] = field(default_factory=dict)

    def render_generation_context(self) -> str:
        sections = [
            "Node generation request context (authoritative):",
            f"- node: {self.node_meta.name}",
            "- global requirement markdown is available to the node coder and is repeated below:",
            self.requirement_text,
            "- node input/output formats:",
            json.dumps(dict(self.node_format), ensure_ascii=False, indent=2, default=str),
        ]
        if self.dependency_context:
            sections.append(self.dependency_context)
        return "\n".join(sections)


@dataclass
class NodeGenerationWorkflow:
    """Reusable ``node_plan -> node_generate -> node_audit`` workflow for any node kind."""

    node_planner: NodePlanElement
    node_coder: PromptNodeFileCoderBase
    node_auditor: NodeAuditor
    _engine: WorkflowEngine | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _node_classes: dict[str, type[WorkflowStepNode]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        # Build before publishing this workflow to concurrent callers. The pool
        # owns run concurrency; no lazy first-run initialization race is needed.
        self.init_node_engine()

    @staticmethod
    def json_config() -> dict[str, Any]:
        """Load the generation topology and pool_size from workflow JSON."""
        config = parse_json_file("node_generation.json", Path(__file__).with_name("configs"))
        if not isinstance(config, dict):
            raise TypeError("Node generation configuration must be a JSON object")
        return config

    def init_node_engine(self) -> WorkflowEngine:
        """Build once from exact classes and shallow dataclass settings snapshots.

        Later component replacements or instance monkeypatches cannot change
        native pool nodes. Per-request settings belong in the request state.
        """
        if self._closed:
            raise RuntimeError("node generation workflow is closed")
        if self._engine is None:
            NODES = (type(self.node_planner), type(self.node_coder), type(self.node_auditor))
            NODE_CLASSES = {node.STEP_ID: node for node in NODES}
            settings = {
                component.STEP_ID: {
                    setting.name: getattr(component, setting.name)
                    for setting in fields(component) if setting.init
                }
                for component in (self.node_planner, self.node_coder, self.node_auditor)
                if is_dataclass(component)
            }
            settings.setdefault(self.node_auditor.STEP_ID, {})["white_list"] = set(self.node_auditor.white_list)
            self._node_classes = NODE_CLASSES
            with node_settings(settings):
                self._engine = init_workflow_engine(NODE_CLASSES, self.json_config())
        return self._engine

    def close(self) -> None:
        """Release the native pool once; a closed workflow cannot be restarted."""
        self._closed = True
        engine, self._engine = self._engine, None
        if engine is not None:
            close_workflow_engine(engine)

    @staticmethod
    def build_plan_request(request: NodeGenerationRequest) -> dict[str, Any]:
        return {
            "node": dict(request.raw_node),
            "requirement_text": request.requirement_text,
            "generate_markdown": request.generate_markdown and not request.markdown_path.is_file(),
            "amendment": request.markdown_amendment,
            "output_path": str(request.markdown_path),
            "temperature": min(request.temperature, 0.2),
        }

    def build_node_request(
        self,
        request: NodeGenerationRequest,
        plan_request: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the seed for WorkflowParam.state consumed by the native steps."""

        node_meta, target_path = request.node_meta, request.target_path
        if node_meta is None or target_path is None:
            raise ValueError("node generation requires node_meta and target_path")
        node_name = node_meta.name
        generation_context = request.render_generation_context()
        amendment = request.node_amendment
        action = "amend" if amendment and target_path.is_file() else "write"
        if amendment and action == "write":
            generation_context += f"\n\nRequested amendment (apply while generating):\n{amendment}\n"

        return {
            "node_plan_request": plan_request,
            "node_writer": {
                "node_name": node_name, "node_meta": node_meta,
                "requirement_md_path": request.requirement_md_path,
                "output_path": str(target_path), "code_path": str(target_path), "amendment": amendment,
                "graph_plan_path": request.graph_plan_path,
                "language": request.language, "temperature": request.temperature,
                "root_dir_path": str(Path(request.graph_plan_path).parent),
                "additional_generation_context": generation_context,
            },
            f"{self._node_classes['node_generate'].__name__}::action": action,
            "auditor": {
                "file_path": str(target_path), "node_meta": node_meta,
                "graph_plan_path": request.graph_plan_path,
            },
        }

    def run(
        self,
        request: NodeGenerationRequest,
    ) -> str:
        """Generate and audit one node, returning the generated file path."""

        engine = self._engine
        if engine is None:
            raise RuntimeError("node generation workflow is closed or not initialized")
        if request.node_meta is None or not request.node_meta.name or request.target_path is None:
            raise ValueError("node generation requires named node_meta and target_path")
        node_name = request.node_meta.name
        if request.raw_node.get("name") != node_name:
            raise ValueError("node generation raw_node.name must match node_meta.name")
        if request.markdown_path is None:
            raise ValueError("node generation requires markdown_path")
        plan_request = self.build_plan_request(request)
        needs_plan = not request.skip_node_planner and (
            plan_request["generate_markdown"] or bool(plan_request["amendment"])
        )
        run_result = run_workflow(
            engine,
            state={
                **request.state,
                **self.build_node_request(request, plan_request),
            },
            skipped_steps=() if needs_plan else ("node_plan",),
        )
        outputs = run_result.outputs
        result = outputs["node_audit"].derived
        if not result["ok"]:
            feedback = "\n".join(
                f"Line {item['lineno']}: {item['rule']} - {item['detail']}"
                for item in result["violations"]
            )
            raise RuntimeError(f"node audit did not pass for {node_name}. Feedback:\n{feedback}")
        return outputs["node_generate"].derived["generated_path"]


__all__ = ["NodeGenerationRequest", "NodeGenerationWorkflow"]
