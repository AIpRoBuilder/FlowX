"""Dependency-aware workflow for generating and amending graph nodes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ag_ui_workflow import StepRunOutput, WorkflowEngine

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.architect.graph import NodeMeta
from flowx_core.tools.workflow_engine import init_pipeline_engine, run_workflow_step
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


WorkflowCallback = Callable[..., Any]


@dataclass
class NodeGenerationContext:
    """Mutable state shared by every node generator in one request."""

    graph_plan_path: str
    requirement_md_path: str
    requirement_text: str
    node_names: list[str]
    selected_node_names: list[str]
    language: str
    temperature: float
    node_docs_dir: Path
    generate_markdowns: bool = True
    markdown_amendments: Mapping[str, str] = field(default_factory=dict)
    node_amendments: Mapping[str, str] = field(default_factory=dict)
    workflow_requests: Mapping[str, Any] = field(default_factory=dict)
    workflow_registry: Mapping[str, WorkflowCallback] = field(default_factory=dict)
    node_formats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    raw_nodes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_node_names: list[str] = field(default_factory=list)

    def render_generation_context(
        self,
        node_name: str,
        prior_artifacts: Mapping[str, Mapping[str, Any]],
    ) -> str:
        formats = self.node_formats.get(node_name, {})
        sections = [
            "Node generation request context (authoritative):",
            f"- node: {node_name}",
            "- global requirement markdown is available to the node coder and is repeated below:",
            self.requirement_text,
            "- node input/output formats:",
            json.dumps(dict(formats), ensure_ascii=False, indent=2, default=str),
        ]
        if prior_artifacts:
            sections.append(
                "- previously generated node artifacts (use as authoritative dependency/design context):"
            )
            for prior_name, artifact in prior_artifacts.items():
                sections.extend(
                    [
                        f"\n### {prior_name}",
                        f"Markdown:\n{artifact.get('node_markdown', '')}",
                        f"Generated file:\n{artifact.get('node_file', '')}",
                    ]
                )
        return "\n".join(sections)


@dataclass
class NodeGenerationWorkflow:
    """Single-node ``node_plan -> node_generate -> node_audit`` workflow for one meta_node_kind."""

    meta_node_kind: str
    node_planner: NodePlanElement
    node_coder: PromptNodeFileCoderBase
    node_auditor: NodeAuditor
    _engine: WorkflowEngine | None = field(default=None, init=False, repr=False)

    def init_node_engine(self, needs_plan: bool) -> WorkflowEngine:
        steps: dict[str, Any] = {"node_plan": self.node_planner} if needs_plan else {}
        steps.update(node_generate=self.node_coder, node_audit=self.node_auditor)
        return init_pipeline_engine(
            steps,
            {"node_generate": ["node_plan"] if needs_plan else [], "node_audit": ["node_generate"]},
        )

    @staticmethod
    def build_plan_request(context: NodeGenerationContext, node_name: str) -> dict[str, Any]:
        markdown_file = context.node_docs_dir / f"{node_name}.md"
        return {
            "node": dict(context.raw_nodes[node_name]),
            "requirement_text": context.requirement_text,
            "generate_markdown": context.generate_markdowns and not markdown_file.is_file(),
            "amendment": context.markdown_amendments.get(node_name, ""),
        }

    def build_node_request(
        self,
        context: NodeGenerationContext,
        node_meta: NodeMeta,
        node_name: str,
        target_path: Path,
        plan_request: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the session_state consumed by the plan, generate and audit steps."""

        preceding_names = set(context.node_names[:context.node_names.index(node_name)])
        generation_context = context.render_generation_context(
            node_name, {name: artifact for name, artifact in context.artifacts.items() if name in preceding_names},
        )
        amendment = context.node_amendments.get(node_name, "")
        action = "amend" if amendment and target_path.is_file() else "write"
        if amendment and action == "write":
            generation_context += f"\n\nRequested amendment (apply while generating):\n{amendment}\n"

        return {
            "node_plan_request": plan_request,
            "node_writer": {
                "node_name": node_name, "node_meta": node_meta,
                "requirement_md_path": context.requirement_md_path,
                "output_path": str(target_path), "code_path": str(target_path), "amendment": amendment,
                "graph_plan_path": context.graph_plan_path,
                "language": context.language, "temperature": context.temperature,
                "root_dir_path": str(Path(context.graph_plan_path).parent),
                "additional_generation_context": generation_context,
            },
            f"{type(self.node_coder).__name__}::action": action,
            "auditor": {
                "file_path": str(target_path), "node_meta": node_meta,
                "graph_plan_path": context.graph_plan_path,
            },
        }

    def run(
        self,
        context: NodeGenerationContext,
        node_meta: NodeMeta,
        node_name: str,
        target_path: Path,
    ) -> str:
        """Generate and audit one node, returning the generated file path."""

        plan_request = self.build_plan_request(context, node_name)
        needs_plan = plan_request["generate_markdown"] or bool(plan_request["amendment"])
        self.node_planner.output_path = str(context.node_docs_dir / f"{node_name}.md")
        self.node_planner.temperature = min(context.temperature, 0.2)
        engine = self._engine = self.init_node_engine(needs_plan)
        engine.session.state.update(
            self.build_node_request(context, node_meta, node_name, target_path, plan_request)
        )
        for step in engine.steps_meta:
            run_workflow_step(engine, step["id"])

        outputs = engine.session.step_outputs
        result = outputs["node_audit"].derived
        if not result["ok"]:
            feedback = "\n".join(
                f"Line {item['lineno']}: {item['rule']} - {item['detail']}"
                for item in result["violations"]
            )
            raise RuntimeError(f"node audit did not pass for {node_name}. Feedback:\n{feedback}")
        return outputs["node_generate"].derived["generated_path"]


__all__ = ["NodeGenerationContext", "NodeGenerationWorkflow"]
