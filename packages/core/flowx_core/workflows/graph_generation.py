"""Single-pass requirement analysis, graph planning/amendment, and graph auditing.

Modified 2026-10-14: extract graph building into a request-controlled workflow.
Modified 2026-10-14: initialize once and seed native state from each caller's request.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, Mapping

from ag_ui_workflow import RunResult, WorkflowEngine

from flowx_core.architect.graph_planner import GraphPlanner
from flowx_core.auditor.graph_json_auditor import GraphJsonAuditor
from flowx_core.demand_analyzer.requirement_disector import RequirementDisector
from flowx_core.tools._node_settings import node_settings
from flowx_core.tools.file_tools import parse_json_file
from flowx_core.tools.workflow_engine import (
    close_workflow_engine,
    init_workflow_engine,
    run_workflow,
)


@dataclass(frozen=True, kw_only=True)
class GraphGenerationRequest:
    """Paths, planner mode, and independent step skips for one graph build."""

    requirement_text: str = ""
    requirement_md_path: str | None = None
    graph_plan_path: str | None = None
    graph_action: Literal["plan", "amend"] = "plan"
    amendment: str = ""
    temperature: float = 0.35
    requirement_temperature: float = 0.2
    skip_analyze_requirement: bool = False
    skip_plan_graph: bool = False
    skip_audit_graph: bool = False
    state: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class GraphGenerationWorkflow:
    """Reuse initialized concrete nodes; only WorkflowParam holds run state."""

    analyzer: RequirementDisector
    planner: GraphPlanner
    graph_auditor: GraphJsonAuditor
    _engine: WorkflowEngine | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        components = (self.analyzer, self.planner, self.graph_auditor)
        node_classes = {type(node).STEP_ID: type(node) for node in components}
        settings = {
            node.STEP_ID: {
                setting.name: getattr(node, setting.name)
                for setting in fields(node) if setting.init
            }
            for node in (self.analyzer, self.planner)
        }
        with node_settings(settings):
            self._engine = init_workflow_engine(node_classes, self.json_config())

    def close(self) -> None:
        """Release this service's pool once, after all its requests finish."""
        engine, self._engine = self._engine, None
        if engine is not None:
            close_workflow_engine(engine)

    @staticmethod
    def json_config() -> dict[str, Any]:
        config = parse_json_file("graph_generation.json", Path(__file__).with_name("configs"))
        if not isinstance(config, dict):
            raise TypeError("Graph generation configuration must be a JSON object")
        return config

    def run(self, request: GraphGenerationRequest) -> RunResult:
        """Execute selected steps, raising audit feedback without amending or retrying."""
        engine = self._engine
        if engine is None:
            raise RuntimeError("graph generation workflow is closed")
        if request.graph_action not in ("plan", "amend"):
            raise ValueError("graph_action must be 'plan' or 'amend'")
        skipped_steps = tuple(
            step_id for step_id, skip in (
                (self.analyzer.STEP_ID, request.skip_analyze_requirement),
                (self.planner.STEP_ID, request.skip_plan_graph),
                (self.graph_auditor.STEP_ID, request.skip_audit_graph),
            ) if skip
        )
        result = run_workflow(engine, state={
            **request.state,
            f"{type(self.analyzer).__name__}::action": "write",
            "requirement_disector": {
                "requirement_text": request.requirement_text,
                "output_path": request.requirement_md_path,
                "temperature": request.requirement_temperature,
            },
            f"{type(self.planner).__name__}::action": (
                "write" if request.graph_action == "plan" else "amend"
            ),
            "graph_planner": {
                "requirement_md_path": request.requirement_md_path,
                "output_path": request.graph_plan_path,
                "graph_json_path": request.graph_plan_path,
                "amendment": request.amendment,
                "temperature": request.temperature,
            },
            "graph_auditor": {"graph_plan_json_path": request.graph_plan_path},
        }, skipped_steps=skipped_steps)

        if not request.skip_audit_graph:
            audit = result.outputs[self.graph_auditor.STEP_ID].derived
            if not audit["ok"]:
                feedback = "\n".join(
                    f"Line {item['lineno']}: {item['rule']} - {item['detail']}"
                    for item in audit["violations"]
                )
                raise RuntimeError(f"graph {request.graph_action} audit did not pass. Feedback:\n{feedback}")
        if not request.skip_plan_graph:
            graph_path = result.outputs[self.planner.STEP_ID].derived["graph_plan_json_path"]
            self.planner._write_mermaid_from_graph_json(Path(graph_path))
        return result


__all__ = ["GraphGenerationRequest", "GraphGenerationWorkflow"]