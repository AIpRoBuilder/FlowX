"""Dependency-aware workflow for generating and amending graph nodes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from ag_ui_workflow import StepRunOutput, WorkflowEngine, WorkflowStepNode

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools.node_formats import collect_node_formats
from flowx_core.tools.workflow_engine import (
    build_workflow_config,
    init_workflow_engine,
    run_workflow_step,
)
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


WorkflowCallback = Callable[..., Any]


@dataclass
class NodeGenerationContext:
    """Mutable state shared by every node generator in one request."""

    builder: Any
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


class _NodeGenerationStage:
    """Request-scoped configuration shared by the three per-node workflow stages."""

    def __init__(
        self,
        builder: Any,
        node_name: str,
        context: NodeGenerationContext,
        *,
        total: int = 0,
        node_index: int = 0,
    ) -> None:
        super().__init__()
        self.builder = builder
        self.node_name = node_name
        self.context = context
        self.total = total
        self.node_index = node_index

    def configure(
        self,
        context: NodeGenerationContext,
        *,
        total: int,
        node_index: int,
    ) -> "_NodeGenerationStage":
        self.context = context
        self.total = total
        self.node_index = node_index
        return self


class NodePlanningNode(_NodeGenerationStage, NodePlanElement):
    """Produce or amend the markdown plan for a single graph node."""

    __hash__ = object.__hash__

    def __post_init__(self) -> None:
        # The builder owns the configured planner (including injected test doubles).
        WorkflowStepNode.__init__(self)

    def _node_entry(self) -> dict[str, Any]:
        entry = self.context.raw_nodes.get(self.node_name)
        if entry is None:
            raise ValueError(f"node {self.node_name!r} was not found in the graph plan")
        return dict(entry)

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        markdown_path = self.context.node_docs_dir / f"{self.node_name}.md"
        amendment = self.context.markdown_amendments.get(self.node_name, "")
        if not amendment and (markdown_path.is_file() or not self.context.generate_markdowns):
            return StepRunOutput(derived={"markdown_path": str(markdown_path.resolve()) if markdown_path.is_file() else None})
        planner = self.builder.node_planner
        planner.node = self._node_entry()
        planner.requirement_text = self.context.requirement_text
        planner.output_path = str(markdown_path)
        planner.index = self.context.node_names.index(self.node_name) + 1
        planner.temperature = min(self.context.temperature, 0.2)
        session_state["node_plan_request"] = {
            "generate_markdown": self.context.generate_markdowns,
            "amendment": amendment,
        }
        return planner.process_input(
            user_input,
            dependency_results,
            session_state,
        )


class NodeWritingNode(_NodeGenerationStage, PromptNodeFileCoderBase):
    """Generate node code using the metadata-selected specialist coder."""

    __hash__ = object.__hash__

    def __post_init__(self) -> None:
        # Specialist coders are selected once the node metadata is known.
        WorkflowStepNode.__init__(self)
        self._current_coder: Any = None
        self._current_node_meta: Any = None
        self.last_generated_path: str | None = None

    def get_node_contract_text(self) -> str:
        return self._current_coder.get_node_contract_text()

    def get_feedback_contract_text(self) -> str:
        return self._current_coder.get_feedback_contract_text()

    def _amend(self, amendment: str, _audit_round: int = 0) -> None:
        del _audit_round
        if self._current_coder is None or not self.last_generated_path:
            raise RuntimeError(f"node writer is not initialized for {self.node_name}")
        self._current_coder.amend_code_with_feedback(
            self.last_generated_path,
            amendment,
            graph_plan_path=self.context.graph_plan_path,
            requirement_md_path=self.context.requirement_md_path,
            current_node_name=self.node_name,
            language=self.context.language,
            temperature=self.context.temperature,
        )

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input, dependency_results, session_state
        self.last_generated_path = None
        self._current_coder = None
        node_meta = self.builder.planned_graph.get_node_meta(self.node_name)
        coder = self.builder._make_node_coder(node_meta)
        self.builder._sync_node_coder_root_dir(coder)
        current_index = self.context.node_names.index(self.node_name)
        preceding_names = set(self.context.node_names[:current_index])
        prior_artifacts = {
            name: dict(artifact)
            for name, artifact in self.context.artifacts.items()
            if name in preceding_names
        }
        generation_context = self.context.render_generation_context(self.node_name, prior_artifacts)
        markdown_path = self.context.node_docs_dir / f"{self.node_name}.md"
        if markdown_path.is_file():
            generation_context += (
                "\n\nCurrent node markdown plan (authoritative for implementation):\n"
                f"{markdown_path.read_text(encoding='utf-8')}\n"
            )
        coder.additional_generation_context = generation_context
        target_path = self.builder._expected_backend_node_path(self.node_name, self.context.language)
        self.builder._logger.info(
            "[%s/%s] Generating node '%s' -> %s",
            self.node_index, self.total, self.node_name, target_path,
        )
        file_path = coder.write_node_from_requirement(
            self.node_name,
            node_meta,
            self.context.requirement_md_path,
            str(target_path),
            graph_plan_path=self.context.graph_plan_path,
            language=self.context.language,
            temperature=self.context.temperature,
        )
        self._current_coder = coder
        self._current_node_meta = node_meta
        self.last_generated_path = str(file_path)
        amendment = self.context.node_amendments.get(self.node_name)
        if amendment:
            self._amend(amendment)
        return StepRunOutput(derived={"generated_path": self.last_generated_path})


class NodeAuditingNode(_NodeGenerationStage, NodeAuditor):
    """Audit, repair and publish the generated node as the final stage."""

    def __init__(self, builder: Any, node_name: str, context: NodeGenerationContext, *, writer: NodeWritingNode, **kwargs: Any) -> None:
        super().__init__(builder, node_name, context, **kwargs)
        self.writer = writer
        self.last_generated_path: str | None = None
        self.last_audit_error: RuntimeError | None = None

    def _audit(self) -> tuple[bool, list[Any]]:
        return self.builder.node_auditor.audit_node_file(
            self.writer.last_generated_path,
            self.writer._current_node_meta,
            graph_plan_path=self.context.graph_plan_path,
        )

    def _amend(self, amendment: str, _audit_round: int = 0) -> None:
        self.writer._amend(amendment, _audit_round)

    def _retry_log(self, amendment: str, _audit_round: int) -> None:
        self.builder._logger.warning(
            "[%s/%s] Node audit failed: %s. %s Applying amendment...",
            self.node_index, self.total, self.node_name, amendment,
        )

    def _requested_workflow_names(self) -> list[str]:
        request = self.context.workflow_requests.get(self.node_name)
        if request is None or request is False:
            return []
        if request is True:
            return list(self.context.workflow_registry)
        if isinstance(request, str):
            names = [request]
        elif isinstance(request, Sequence) and not isinstance(request, (str, bytes)):
            names = [str(item) for item in request]
        else:
            raise TypeError(
                f"workflow request for {self.node_name!r} must be a bool, name, or sequence of names"
            )
        unknown = set(names).difference(self.context.workflow_registry)
        if unknown:
            raise ValueError(
                f"unknown workflow(s) requested for {self.node_name}: {sorted(unknown)}"
            )
        return names

    def _run_requested_workflows(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for workflow_name in self._requested_workflow_names():
            outputs[workflow_name] = self.context.workflow_registry[workflow_name](
                node_name=self.node_name,
                artifact=dict(artifact),
                context=self.context,
                builder=self.context.builder,
            )
        return outputs

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input, dependency_results, session_state
        node_name = self.node_name
        self.last_generated_path = None
        self.last_audit_error = None
        generated_path = self.writer.last_generated_path
        if generated_path is None or self.writer._current_coder is None:
            raise RuntimeError(f"node writer did not produce a file for {node_name}")
        repair_loop = self.builder._make_audit_repair_loop()
        try:
            repair_loop.run(
                audit=self._audit,
                amend=self._amend,
                failure_message_prefix=(
                    f"node audit did not pass for {node_name} after "
                    f"{repair_loop.max_attempts} attempt(s). Last feedback:\n"
                ),
                on_success=lambda _audit_round: self.builder._logger.info(
                    "[%s/%s] Node audit passed: %s",
                    self.node_index, self.total, node_name,
                ),
                on_retry=self._retry_log,
            )
        except RuntimeError as exc:
            self.last_audit_error = exc
            raise
        self.builder.node_coder_map[node_name] = self.writer._current_coder
        self.builder.node_location_map[node_name] = generated_path
        self.last_generated_path = generated_path
        markdown_file = self.context.node_docs_dir / f"{node_name}.md"
        markdown_path = str(markdown_file.resolve()) if markdown_file.is_file() else None
        if markdown_path:
            self.builder.node_docs_dir = str(markdown_file.parent)
            paths = [
                path for path in (self.builder.node_doc_paths or [])
                if Path(path).name != markdown_file.name
            ]
            paths.append(markdown_path)
            self.builder.node_doc_paths = paths
        artifact: dict[str, Any] = {
            "node_name": node_name,
            "node_file_path": generated_path,
            "node_markdown_path": markdown_path,
            "node_file": Path(generated_path).read_text(encoding="utf-8") if Path(generated_path).is_file() else "",
            "node_markdown": (
                Path(markdown_path).read_text(encoding="utf-8")
                if markdown_path and Path(markdown_path).is_file()
                else ""
            ),
            "node_formats": dict(self.context.node_formats.get(node_name, {})),
        }
        artifact["workflow_outputs"] = self._run_requested_workflows(artifact)
        self.context.artifacts[node_name] = artifact
        self.builder._advance_progress(f"Generated node: {node_name}")
        return StepRunOutput(
            card={"kind": "node-generation", "status": "completed"},
            derived={
                "generated_path": generated_path,
                "markdown_path": markdown_path,
                "workflow_outputs": artifact["workflow_outputs"],
            },
        )


@dataclass
class NodeGenerationWorkflow:
    """Build a planner → writer → auditor pipeline for every selected graph node."""

    builder: Any
    planners: MutableMapping[str, NodePlanningNode] = field(default_factory=dict)
    writers: MutableMapping[str, NodeWritingNode] = field(default_factory=dict)
    nodes: MutableMapping[str, NodeAuditingNode] = field(default_factory=dict)
    _engine: WorkflowEngine | None = field(default=None, init=False, repr=False)
    _context: NodeGenerationContext | None = field(default=None, init=False, repr=False)

    @property
    def context(self) -> NodeGenerationContext | None:
        """Return the most recent request context and its generated artifacts."""

        return self._context

    @staticmethod
    def _normalize_requested_names(
        all_names: list[str],
        node_names: Sequence[str] | None,
        run_nodes: Sequence[str] | Mapping[str, bool] | None,
    ) -> list[str]:
        requested = set(all_names if node_names is None else node_names)
        unknown = requested.difference(all_names)
        if unknown:
            raise ValueError(f"node(s) not found in graph plan: {sorted(unknown)}")
        if run_nodes is None:
            selected = requested
        elif isinstance(run_nodes, Mapping):
            unknown_run_names = set(run_nodes).difference(all_names)
            if unknown_run_names:
                raise ValueError(f"node(s) not found in graph plan: {sorted(unknown_run_names)}")
            selected = requested.intersection(name for name, should_run in run_nodes.items() if should_run)
        else:
            run_set = set(run_nodes)
            unknown_run_names = run_set.difference(all_names)
            if unknown_run_names:
                raise ValueError(f"node(s) not found in graph plan: {sorted(unknown_run_names)}")
            selected = requested.intersection(run_set)
        return [name for name in all_names if name in selected]

    @staticmethod
    def _raw_node_map(graph_payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        nodes = graph_payload.get("nodes", [])
        if not isinstance(nodes, list):
            raise ValueError("graph_plan JSON must contain a top-level 'nodes' list")
        result: dict[str, dict[str, Any]] = {}
        for entry in nodes:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name", "")).strip()
            if name:
                result[name] = dict(entry)
        return result

    def _seed_existing_artifacts(
        self,
        context: NodeGenerationContext,
        ordered_names: list[str],
    ) -> None:
        for name in ordered_names:
            markdown_path = context.node_docs_dir / f"{name}.md"
            node_path_value = self.builder.node_location_map.get(name)
            node_path = (
                self.builder._resolve_root_path(node_path_value)
                if node_path_value
                else self.builder._expected_backend_node_path(name, context.language)
            )
            if markdown_path.is_file() or node_path.is_file():
                context.artifacts[name] = {
                    "node_name": name,
                    "node_file_path": str(node_path) if node_path.is_file() else "",
                    "node_markdown_path": str(markdown_path) if markdown_path.is_file() else "",
                    "node_file": node_path.read_text(encoding="utf-8") if node_path.is_file() else "",
                    "node_markdown": markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else "",
                    "node_formats": dict(context.node_formats.get(name, {})),
                }

    def run(
        self,
        *,
        graph_plan_path: str | None = None,
        requirement_md_path: str | None = None,
        node_names: Sequence[str] | None = None,
        run_nodes: Sequence[str] | Mapping[str, bool] | None = None,
        language: str = "python",
        temperature: float = 0.35,
        node_docs_dirname: str = "node_docs",
        generate_markdowns: bool = True,
        markdown_amendments: Mapping[str, str] | None = None,
        node_amendments: Mapping[str, str] | None = None,
        workflow_requests: Mapping[str, Any] | None = None,
        workflow_registry: Mapping[str, WorkflowCallback] | None = None,
        reset_mappings: bool = False,
    ) -> NodeGenerationContext:
        if graph_plan_path:
            self.builder.graph_plan_path = graph_plan_path
        planned_graph = self.builder._load_planned_graph()
        selected_graph_path = Path(self.builder.graph_plan_path).expanduser().resolve()
        graph_payload = json.loads(selected_graph_path.read_text(encoding="utf-8"))
        raw_nodes = self._raw_node_map(graph_payload)
        all_names = planned_graph.get_topological_sorted_nodes()
        ordered_names = self._normalize_requested_names(all_names, node_names, run_nodes)

        if requirement_md_path:
            self.builder.requirement_md_path = requirement_md_path
        requirement_text = self.builder._read_requirement_text()
        if reset_mappings:
            self.builder.node_coder_map = {}
            self.builder.node_location_map = {}

        docs_dir = self.builder._resolve_root_path(node_docs_dirname)
        docs_dir.mkdir(parents=True, exist_ok=True)
        try:
            formats = collect_node_formats(self.builder, planned_graph, language)
        except Exception:
            formats = {}

        context = NodeGenerationContext(
            builder=self.builder,
            graph_plan_path=str(selected_graph_path),
            requirement_md_path=str(Path(self.builder.requirement_md_path).expanduser().resolve()),
            requirement_text=requirement_text,
            node_names=all_names,
            selected_node_names=ordered_names,
            language=language,
            temperature=temperature,
            node_docs_dir=docs_dir,
            generate_markdowns=generate_markdowns,
            markdown_amendments=markdown_amendments or {},
            node_amendments=node_amendments or {},
            workflow_requests=workflow_requests or {},
            workflow_registry=workflow_registry or {},
            node_formats=formats,
        )
        context.raw_nodes = raw_nodes
        context.skipped_node_names = [name for name in all_names if name not in set(ordered_names)]
        self._context = context
        self._seed_existing_artifacts(context, context.skipped_node_names)
        if not ordered_names:
            self._engine = None
            return context

        steps: dict[str, WorkflowStepNode] = {}
        dependencies: dict[str, list[str]] = {}
        selected_names = set(ordered_names)
        previous_selected: str | None = None
        for index, node_name in enumerate(ordered_names, start=1):
            planner = self.planners.get(node_name)
            if planner is None:
                planner = NodePlanningNode(self.builder, node_name, context)
                self.planners[node_name] = planner
            writer = self.writers.get(node_name)
            if writer is None:
                writer = NodeWritingNode(self.builder, node_name, context)
                self.writers[node_name] = writer
            auditor = self.nodes.get(node_name)
            if auditor is None:
                auditor = NodeAuditingNode(self.builder, node_name, context, writer=writer)
                self.nodes[node_name] = auditor
            for stage in (planner, writer, auditor):
                stage.configure(context, total=len(ordered_names), node_index=index)

            plan_id = f"{node_name}::plan"
            write_id = f"{node_name}::write"
            audit_id = f"{node_name}::audit"
            steps[plan_id] = planner
            steps[write_id] = writer
            steps[audit_id] = auditor
            node_dependencies = [
                f"{dependency}::audit"
                for dependency in planned_graph.get_node_dependencies(node_name)
                if dependency in selected_names
            ]
            if previous_selected and f"{previous_selected}::audit" not in node_dependencies:
                node_dependencies.append(f"{previous_selected}::audit")
            dependencies[plan_id] = node_dependencies
            dependencies[write_id] = [plan_id]
            dependencies[audit_id] = [write_id]
            previous_selected = node_name

        self.builder._start_progress(len(ordered_names))
        config = build_workflow_config(steps, dependencies)
        self._engine = init_workflow_engine(config)
        for step_id in steps:
            try:
                run_workflow_step(self._engine, step_id)
            except RuntimeError as exc:
                if step_id.endswith("::audit"):
                    auditor = self.nodes[step_id.removesuffix("::audit")]
                    if auditor.last_audit_error is not None:
                        raise auditor.last_audit_error from exc
                raise
        return context


__all__ = [
    "NodeGenerationContext",
    "NodePlanningNode",
    "NodeWritingNode",
    "NodeAuditingNode",
    "NodeGenerationWorkflow",
]
