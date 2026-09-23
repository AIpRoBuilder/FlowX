from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

from pydaograph import CStatus, GNode, GPipeline  # pyright: ignore[reportMissingImports]

from flowx_core.llm_client.coder import MAX_TOKENS
from flowx_core.runtime import RunNodeTest
from flowx_core.tools.file_tools import compile_node_file_and_get_step_output_card_schema
from flowx_core.tools.runnable_gnode import run_gnode_operation
from flowx_core.tools.workflow_node_reference import resolve_workflow_node_reference


if TYPE_CHECKING:
    from flowx_core.agent_builder import AgentBuilder


@dataclass
class NodeGenerationWorker:
    builder: "AgentBuilder"
    node_name: str
    total: int = 0
    node_index: int = 0
    language: str = "python"
    temperature: float = 0.35
    last_generated_path: Optional[str] = field(default=None, init=False, repr=False)
    _current_node_meta: Any = field(default=None, init=False, repr=False)
    _current_coder: Any = field(default=None, init=False, repr=False)
    _current_resolved_file_path: Optional[str] = field(default=None, init=False, repr=False)

    def configure(
        self,
        *,
        total: int,
        node_index: int,
        language: str,
        temperature: float,
    ) -> "NodeGenerationWorker":
        self.total = total
        self.node_index = node_index
        self.language = language
        self.temperature = temperature
        return self

    def _audit(self) -> tuple[bool, list[Any]]:
        return run_gnode_operation(
            self.builder.node_auditor,
            "audit_node_file",
            self._current_resolved_file_path,
            self._current_node_meta,
            graph_plan_path=self.builder.graph_plan_path,
        )

    def _retry_log(self, amendment: str, _audit_round: int) -> None:
        self.builder._logger.warning(
            "[%s/%s] Node audit failed: %s. %s Applying amendment...",
            self.node_index,
            self.total,
            self.node_name,
            amendment,
        )

    def _ensure_amendment_context(self) -> None:
        if self._current_coder is None:
            self._current_coder = self.builder._ensure_node_coder(self.node_name)
        if self._current_coder is None:
            raise RuntimeError(f"unable to resolve coder for node {self.node_name}")

        if not self._current_resolved_file_path:
            self._current_resolved_file_path = self.builder.node_location_map.get(self.node_name)
        if not self._current_resolved_file_path:
            self._current_resolved_file_path = str(
                self.builder._expected_backend_node_path(self.node_name, self.language)
            )

    def _amend(self, amendment: str, _audit_round: int = 0) -> None:
        del _audit_round
        self._ensure_amendment_context()
        run_gnode_operation(
            self._current_coder,
            "amend_code_with_feedback",
            self._current_resolved_file_path,
            amendment,
            graph_plan_path=self.builder.graph_plan_path or "",
            requirement_md_path=self.builder.requirement_md_path or "",
            current_node_name=self.node_name,
            language=self.language,
            temperature=self.temperature,
        )

    def generate(self) -> str:
        try:
            node_meta = self.builder.planned_graph.get_node_meta(self.node_name)
            coder = self.builder._make_node_coder(node_meta)
            self.builder._sync_node_coder_root_dir(coder)
            target_path = self.builder._expected_backend_node_path(self.node_name, self.language)
            self.builder._logger.info(
                "[%s/%s] Generating node '%s' -> %s",
                self.node_index,
                self.total,
                self.node_name,
                target_path,
            )

            file_path = run_gnode_operation(
                coder,
                "write_node_from_requirement",
                self.node_name,
                node_meta,
                self.builder.requirement_md_path,
                str(target_path),
                graph_plan_path=self.builder.graph_plan_path,
                language=self.language,
                temperature=self.temperature,
            )
            resolved_file_path = str(file_path)
            self._current_node_meta = node_meta
            self._current_coder = coder
            self._current_resolved_file_path = resolved_file_path

            repair_loop = self.builder._make_audit_repair_loop()

            repair_loop.run(
                audit=self._audit,
                amend=self._amend,
                failure_message_prefix=(
                    f"node audit did not pass for {self.node_name} after "
                    f"{repair_loop.max_attempts} attempt(s). Last feedback:\n"
                ),
                on_success=lambda _audit_round: self.builder._logger.info(
                    "[%s/%s] Node audit passed: %s",
                    self.node_index,
                    self.total,
                    self.node_name,
                ),
                on_retry=self._retry_log,
            )

            self.builder.node_coder_map[self.node_name] = coder
            self.builder.node_location_map[self.node_name] = resolved_file_path
            self.last_generated_path = resolved_file_path
            return resolved_file_path
        except Exception as exc:
            self.builder._logger.error(
                "[%s/%s] Node generation failed for %s: %s",
                self.node_index,
                self.total,
                self.node_name,
                exc,
                exc_info=True,
            )
            raise RuntimeError(f"node generation failed for {self.node_name}: {exc}") from exc


class _NodeGenerationNode(GNode):
    """Fresh PyDaoGraph node wrapper for a cached generation worker."""

    def __init__(self, worker: NodeGenerationWorker) -> None:
        super().__init__()
        self.worker = worker

    def run(self) -> Any:
        try:
            self.worker.generate()
            self.worker.builder._advance_progress(f"Generated node: {self.worker.node_name}")
            return CStatus()
        except Exception as exc:
            return CStatus(1001, f"node generation failed for {self.worker.node_name}: {exc}")


@dataclass
class NodeTestGenerationWorker:
    builder: "AgentBuilder"
    node_name: str
    node_file_path: str
    total: int
    node_index: int
    language: str
    temperature: float

    def run(self) -> str:
        try:
            test_coder = self.builder._make_node_test_coder()
            target_path = self.builder._expected_backend_node_test_path(self.node_name, self.language)
            self.builder._logger.info(
                "[%s/%s] Generating node test '%s' -> %s",
                self.node_index,
                self.total,
                self.node_name,
                target_path,
            )
            file_path = run_gnode_operation(
                test_coder,
                "write_test_from_node_file",
                self.node_file_path,
                str(target_path),
                temperature=self.temperature,
            )
            return str(file_path)
        except Exception as exc:
            self.builder._logger.error(
                "[%s/%s] Node test generation failed for %s: %s",
                self.node_index,
                self.total,
                self.node_name,
                exc,
                exc_info=True,
            )
            raise RuntimeError(f"node test generation failed for {self.node_name}: {exc}") from exc


@dataclass
class GraphBuildService:
    builder: "AgentBuilder"

    def analyze_requirement(
        self,
        requirement_text: Optional[str] = None,
        requirement_file: Optional[str] = None,
        out_file: str = "requirement_analysis.md",
    ) -> str:
        if requirement_file:
            self.builder.requirement_md_path = requirement_file
            self.builder.requirement_analysis_result = None
            self.builder._logger.info("Using existing requirement file -> %s", requirement_file)
            return requirement_file

        out_path = os.path.join(self.builder.root_dir, out_file)
        self.builder._logger.info("Analyzing requirement -> %s", out_path)
        self.builder._logger.debug(
            "Requirement analysis input length=%s output_file=%s",
            len(requirement_text or ""),
            out_path,
        )
        result = run_gnode_operation(self.builder.analyzer, "analyze", requirement_text or "", out_path)
        self.builder.requirement_md_path = str(result.output_path)
        self.builder.requirement_analysis_result = {
            "output_path": str(result.output_path),
            "is_cron_task": result.is_cron_task,
            "task_type": result.task_type,
            "crontab_expression": result.crontab_expression,
        }
        return self.builder.requirement_md_path

    def plan_graph(
        self,
        requirement_md_path: Optional[str] = None,
        graph_plan_filename: str = "workflow.json",
        temperature: float = 0.35,
    ) -> str:
        if requirement_md_path:
            self.builder.requirement_md_path = requirement_md_path
        if not self.builder.requirement_md_path:
            raise ValueError("requirement_md_path is not set. Call analyze_requirement(...) first or pass requirement_md_path.")

        self.builder.graph_plan_path = os.path.join(self.builder.root_dir, graph_plan_filename)
        self.builder._logger.info("Planning graph -> %s", self.builder.graph_plan_path)
        run_gnode_operation(
            self.builder.planner,
            "plan_from_file",
            self.builder.requirement_md_path,
            self.builder.graph_plan_path,
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            self.builder.planned_graph = self.builder._instantiate_graph(self.builder.graph_plan_path)
            return run_gnode_operation(self.builder.graph_auditor, "audit_graph_json", self.builder.planned_graph)

        def _retry_log(amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning("Graph audit failed. Applying amendment %s...", amendment)

        def _amend(amendment: str, _audit_round: int) -> None:
            run_gnode_operation(
                self.builder.planner,
                "amend_file_with_feedback",
                self.builder.graph_plan_path,
                amendment,
                temperature=temperature,
            )

        repair_loop.run(
            audit=_audit,
            amend=_amend,
            failure_message_prefix=(
                "graph plan audit did not pass after "
                f"{repair_loop.max_attempts} attempt(s). Last feedback:\n"
            ),
            on_success=lambda _audit_round: self.builder._logger.info("Graph plan audit passed."),
            on_retry=_retry_log,
        )
        run_gnode_operation(
            self.builder.planner,
            "_write_mermaid_from_graph_json",
            Path(self.builder.graph_plan_path),
        )
        return self.builder.graph_plan_path

    def amend_graph(
        self,
        amendment: str,
        graph_plan_path: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        if graph_plan_path:
            self.builder.graph_plan_path = graph_plan_path
        if not self.builder.graph_plan_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")
        if not isinstance(amendment, str) or not amendment.strip():
            raise ValueError("amendment must be a non-empty string.")

        run_gnode_operation(
            self.builder.planner,
            "amend_file_with_feedback",
            self.builder.graph_plan_path,
            amendment,
            temperature=temperature,
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            self.builder.planned_graph = self.builder._instantiate_graph(self.builder.graph_plan_path)
            return run_gnode_operation(self.builder.graph_auditor, "audit_graph_json", self.builder.planned_graph)

        def _retry_log(_amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning("Graph amendment audit failed. Applying amendment...")

        def _amend(next_amendment: str, _audit_round: int) -> None:
            run_gnode_operation(
                self.builder.planner,
                "amend_file_with_feedback",
                self.builder.graph_plan_path,
                next_amendment,
                temperature=temperature,
            )

        repair_loop.run(
            audit=_audit,
            amend=_amend,
            failure_message_prefix=(
                "graph amendment audit did not pass after "
                f"{repair_loop.max_attempts} attempt(s). Last feedback:\n"
            ),
            on_success=lambda _audit_round: self.builder._logger.info("Graph amendment audit passed."),
            on_retry=_retry_log,
        )

        run_gnode_operation(
            self.builder.planner,
            "_write_mermaid_from_graph_json",
            Path(self.builder.graph_plan_path),
        )
        return self.builder.graph_plan_path


@dataclass
class NodeBuildService:
    builder: "AgentBuilder"
    nodes: Dict[str, NodeGenerationWorker] = field(default_factory=dict)

    def _resolve_requested_node_names(
        self,
        node_names: Optional[list[str]] = None,
        *,
        graph_plan_path: Optional[str] = None,
    ) -> tuple[Any, list[str]]:
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        topological_names = planned_graph.get_topological_sorted_nodes()

        if node_names is None:
            return planned_graph, topological_names

        requested_names = {name for name in node_names if isinstance(name, str) and name.strip()}
        ordered_names = [name for name in topological_names if name in requested_names]
        missing_names = requested_names.difference(ordered_names)
        if missing_names:
            raise ValueError(f"node(s) not found in graph plan: {sorted(missing_names)}")
        return planned_graph, ordered_names

    def get_or_create_node_worker(
        self,
        node_name: str,
        *,
        total: int,
        node_index: int,
        language: str,
        temperature: float,
    ) -> NodeGenerationWorker:
        worker = self.nodes.get(node_name)
        if worker is None:
            worker = NodeGenerationWorker(
                builder=self.builder,
                node_name=node_name,
            )
            self.nodes[node_name] = worker

        return worker.configure(
            total=total,
            node_index=node_index,
            language=language,
            temperature=temperature,
        )

    def run_node_generation_pipeline(
        self,
        node_names: Optional[list[str]] = None,
        *,
        graph_plan_path: Optional[str] = None,
        language: str = "python",
        temperature: float = 0.35,
        reset_mappings: bool = False,
    ) -> list[str]:
        planned_graph, ordered_names = self._resolve_requested_node_names(
            node_names,
            graph_plan_path=graph_plan_path,
        )
        if reset_mappings:
            self.builder.node_coder_map = {}
            self.builder.node_location_map = {}
        if not ordered_names:
            return []

        pipeline = GPipeline()
        workers: Dict[str, NodeGenerationWorker] = {}
        pipeline_nodes: Dict[str, _NodeGenerationNode] = {}
        selected_names = set(ordered_names)
        total = len(ordered_names)

        for index, node_name in enumerate(ordered_names, start=1):
            worker = self.get_or_create_node_worker(
                node_name,
                total=total,
                node_index=index,
                language=language,
                temperature=temperature,
            )
            workers[node_name] = worker
            pipeline_nodes[node_name] = _NodeGenerationNode(worker)

        for node_name in ordered_names:
            dep_nodes = {
                pipeline_nodes[dep_name]
                for dep_name in planned_graph.get_node_dependencies(node_name)
                if dep_name in selected_names
            }
            status = pipeline.registerGElement(pipeline_nodes[node_name], dep_nodes, node_name, 1)
            if status.isErr():
                raise RuntimeError(f"registerGElement failed for {node_name}: {status.getInfo()}")

        self.builder._start_progress(total)
        process_status = pipeline.process()
        if process_status.isErr():
            raise RuntimeError(f"node generation pipeline failed: {process_status.getInfo()}")

        return [
            self.builder.node_location_map[name]
            for name in ordered_names
            if name in self.builder.node_location_map
        ]

    def generate_selected_node_tests(
        self,
        node_names: list[str],
        *,
        language: str = "python",
        temperature: float = 0.2,
    ) -> list[str]:
        if not self.builder._supports_backend_node_tests(language):
            self.builder._logger.info(
                "Skipping single-node test generation for unsupported backend language: %s",
                language,
            )
            self.builder.dynamic_graph_cache["node_tests"] = {}
            return []

        planned_graph = self.builder._load_planned_graph()
        requested_names = {name for name in node_names if isinstance(name, str) and name.strip()}
        ordered_names = [
            name for name in planned_graph.get_topological_sorted_nodes()
            if name in requested_names
        ]
        missing_names = requested_names.difference(ordered_names)
        if missing_names:
            raise ValueError(f"node(s) not found in graph plan: {sorted(missing_names)}")
        if not ordered_names:
            self.builder.dynamic_graph_cache["node_tests"] = {}
            return []

        generated_map: Dict[str, str] = {}
        total = len(ordered_names)
        for index, name in enumerate(ordered_names, start=1):
            node_file_path = self.builder.node_location_map.get(name)
            if node_file_path:
                resolved_node_path = self.builder._resolve_root_path(node_file_path)
            else:
                resolved_node_path = self.builder._expected_backend_node_path(name, language)
            if not resolved_node_path.is_file():
                raise FileNotFoundError(
                    f"backend node file not found for single-node test generation: {resolved_node_path}"
                )

            generated_map[name] = NodeTestGenerationWorker(
                builder=self.builder,
                node_name=name,
                node_file_path=str(resolved_node_path),
                total=total,
                node_index=index,
                language=language,
                temperature=temperature,
            ).run()

        current_map = self.builder.dynamic_graph_cache.get("node_tests", {})
        merged_map = dict(current_map) if isinstance(current_map, Mapping) else {}
        merged_map.update(generated_map)
        self.builder.dynamic_graph_cache["node_tests"] = merged_map
        return [generated_map[name] for name in ordered_names if name in generated_map]

    def generate_selected_nodes(
        self,
        node_names: list[str],
        *,
        language: str = "python",
        temperature: float = 0.35,
        reset_mappings: bool = False,
    ) -> list[str]:
        return self.run_node_generation_pipeline(
            node_names,
            language=language,
            temperature=temperature,
            reset_mappings=reset_mappings,
        )

    def generate_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        language: str = "python",
        temperature: float = 0.35,
    ) -> list[str]:
        if requirement_md_path:
            self.builder.requirement_md_path = requirement_md_path
        if not self.builder.requirement_md_path:
            raise ValueError("requirement_md_path is not set. Call analyze_requirement(...) first or pass requirement_md_path.")
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        generated_paths = self.generate_selected_nodes(
            planned_graph.get_topological_sorted_nodes(),
            language=language,
            temperature=temperature,
            reset_mappings=True,
        )
        if self.builder._supports_backend_node_tests(language):
            self.builder._generate_selected_node_tests(
                planned_graph.get_topological_sorted_nodes(),
                language=language,
                temperature=temperature,
            )
            self.builder.dynamic_graph_cache["node_tests"] = {
                node_name: str(self.builder._expected_backend_node_test_path(node_name, language))
                for node_name in planned_graph.get_topological_sorted_nodes()
                if self.builder._expected_backend_node_test_path(node_name, language).is_file()
            }
        else:
            self.builder.dynamic_graph_cache["node_tests"] = {}
        return generated_paths

    def generate_node_tests(
        self,
        graph_plan_path: Optional[str] = None,
        backend_language: str = "python",
        temperature: float = 0.2,
    ) -> list[str]:
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        generated_paths = self.builder._generate_selected_node_tests(
            planned_graph.get_topological_sorted_nodes(),
            language=backend_language,
            temperature=temperature,
        )
        if self.builder._supports_backend_node_tests(backend_language):
            self.builder.dynamic_graph_cache["node_tests"] = {
                node_name: str(self.builder._expected_backend_node_test_path(node_name, backend_language))
                for node_name in planned_graph.get_topological_sorted_nodes()
                if self.builder._expected_backend_node_test_path(node_name, backend_language).is_file()
            }
        return generated_paths

    def generate_node_markdowns(
        self,
        requirement_md_path: str,
        graph_plan_path: str,
        output_dirname: str = "node_docs",
        temperature: float = 0.2,
    ) -> list[str]:
        output_dir = os.path.join(self.builder.root_dir, output_dirname)
        self.builder._logger.info("Generating per-node markdown plans -> %s", output_dir)
        node_doc_paths = run_gnode_operation(
            self.builder.node_planner,
            "plan_each_from_files",
            requirement_md_path=requirement_md_path,
            graph_plan_json_path=graph_plan_path,
            output_dir=output_dir,
            overwrite=True,
            temperature=temperature,
        )
        self.builder.node_docs_dir = output_dir
        self.builder.node_doc_paths = [str(path) for path in node_doc_paths]
        return self.builder.node_doc_paths

    def update_nodes_plan(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Dict[str, Any]:
        requirement_text = self.builder._read_requirement_text(requirement_md_path)
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        node_names = planned_graph.get_topological_sorted_nodes()

        node_docs_dir = self.builder._resolve_root_path(node_docs_dirname)
        node_docs_dir.mkdir(parents=True, exist_ok=True)

        existing_plan_map: Dict[str, str] = {}
        missing_plan_names: list[str] = []
        for node_name in node_names:
            plan_path = node_docs_dir / f"{node_name}.md"
            if plan_path.is_file():
                existing_plan_map[node_name] = str(plan_path)
            else:
                missing_plan_names.append(node_name)

        generated_plan_map: Dict[str, str] = {}
        if missing_plan_names:
            filtered_graph_plan = self.builder._build_filtered_graph_plan_payload(missing_plan_names)
            generated_plan_paths = run_gnode_operation(
                self.builder.node_planner,
                "plan_each",
                requirement_text=requirement_text,
                graph_plan_text=json.dumps(filtered_graph_plan, ensure_ascii=False, indent=2),
                output_dir=str(node_docs_dir),
                overwrite=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            generated_plan_map = {Path(path).stem: str(path) for path in generated_plan_paths}

        all_plan_map = {
            node_name: str(node_docs_dir / f"{node_name}.md")
            for node_name in node_names
            if (node_docs_dir / f"{node_name}.md").is_file()
        }

        self.builder.node_docs_dir = str(node_docs_dir)
        self.builder.node_doc_paths = [all_plan_map[node_name] for node_name in node_names if node_name in all_plan_map]

        self.builder.dynamic_graph_cache["graph_nodes"] = node_names
        self.builder.dynamic_graph_cache["graph_plan_path"] = str(Path(self.builder.graph_plan_path).expanduser().resolve())
        self.builder.dynamic_graph_cache["node_plans"] = all_plan_map

        return {
            "graph_plan_path": self.builder.dynamic_graph_cache["graph_plan_path"],
            "node_plan": {
                "existing": existing_plan_map,
                "generated": generated_plan_map,
                "all": all_plan_map,
            },
        }

    def update_backend_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        language: str = "python",
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        plan_result = self.update_nodes_plan(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            node_docs_dirname=node_docs_dirname,
            temperature=temperature,
        )
        planned_graph = self.builder._load_planned_graph()
        node_names = planned_graph.get_topological_sorted_nodes()

        missing_plan_names = [node_name for node_name in node_names if node_name not in plan_result["node_plan"]["all"]]
        if missing_plan_names:
            raise FileNotFoundError(
                "update_backend_nodes requires node plan artifacts before code generation. "
                f"missing node plan files for: {missing_plan_names}"
            )

        existing_backend_map: Dict[str, str] = {}
        missing_backend_names: list[str] = []
        for node_name in node_names:
            backend_path = self.builder._expected_backend_node_path(node_name, language)
            if backend_path.is_file():
                existing_backend_map[node_name] = str(backend_path)
            else:
                missing_backend_names.append(node_name)

        generated_backend_paths = self.builder._generate_selected_nodes(
            missing_backend_names,
            language=language,
            temperature=temperature,
            reset_mappings=False,
        )
        generated_backend_map = {Path(path).stem: str(path) for path in generated_backend_paths}
        all_backend_map = {
            node_name: str(self.builder._expected_backend_node_path(node_name, language))
            for node_name in node_names
            if self.builder._expected_backend_node_path(node_name, language).is_file()
        }
        for node_name, node_path in all_backend_map.items():
            self.builder.node_location_map.setdefault(node_name, node_path)

        self.builder.dynamic_graph_cache["backend_nodes"] = all_backend_map

        existing_test_map: Dict[str, str] = {}
        generated_test_map: Dict[str, str] = {}
        all_test_map: Dict[str, str] = {}
        if self.builder._supports_backend_node_tests(language):
            ordered_missing_test_names: list[str] = []
            for node_name in node_names:
                test_path = self.builder._expected_backend_node_test_path(node_name, language)
                if test_path.is_file():
                    existing_test_map[node_name] = str(test_path)
                else:
                    ordered_missing_test_names.append(node_name)

            generated_test_paths = self.builder._generate_selected_node_tests(
                ordered_missing_test_names,
                language=language,
                temperature=temperature,
            )
            generated_test_map = {
                node_name: generated_test_path
                for node_name, generated_test_path in zip(ordered_missing_test_names, generated_test_paths)
            }
            all_test_map = {
                node_name: str(self.builder._expected_backend_node_test_path(node_name, language))
                for node_name in node_names
                if self.builder._expected_backend_node_test_path(node_name, language).is_file()
            }

        self.builder.dynamic_graph_cache["node_tests"] = all_test_map

        return {
            "node_plan": plan_result["node_plan"],
            "backend_nodes": {
                "existing": existing_backend_map,
                "generated": generated_backend_map,
                "all": all_backend_map,
            },
            "node_tests": {
                "existing": existing_test_map,
                "generated": generated_test_map,
                "all": all_test_map,
            },
        }

    def get_node_input_output_formats(
        self,
        graph_plan_path: Optional[str] = None,
        backend_language: str = "python",
    ) -> Dict[str, Dict[str, Any]]:
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        node_formats: Dict[str, Dict[str, Any]] = {}

        for node_name in planned_graph.get_topological_sorted_nodes():
            node_meta = planned_graph.get_node_meta(node_name)
            inputs_format = node_meta.inputs_format if node_meta and getattr(node_meta, "inputs_format", None) else {}
            if not isinstance(inputs_format, Mapping):
                inputs_format = {}

            normalized_inputs_format: Dict[str, str] = {}
            for key, value in inputs_format.items():
                field_name = str(key).strip()
                field_type = str(value).strip().lower()
                if field_name and field_type:
                    normalized_inputs_format[field_name] = field_type

            ext_data = node_meta.ext_data if node_meta and getattr(node_meta, "ext_data", None) else {}
            if isinstance(ext_data, Mapping):
                ext_type = str(ext_data.get("type", "none")).strip().lower()
            else:
                ext_type = str(ext_data).strip().lower() or "none"
            if ext_type not in {"user_input", "skill"}:
                normalized_inputs_format = {}

            backend_path_value = self.builder.node_location_map.get(node_name)
            if backend_path_value:
                backend_path = self.builder._resolve_root_path(backend_path_value)
            else:
                backend_path = self.builder._expected_backend_node_path(node_name, backend_language)

            backend_card_schema = None
            backend_path_str: Optional[str] = None
            if backend_path.is_file():
                backend_card_schema = compile_node_file_and_get_step_output_card_schema(str(backend_path))
                backend_path_str = str(backend_path)

            node_formats[node_name] = {
                "user_input_format": normalized_inputs_format,
                "backend_output_card_format": backend_card_schema.get("card") if backend_card_schema else None,
                "backend_node_path": backend_path_str,
            }

        self.builder.dynamic_graph_cache["node_input_output_formats"] = node_formats
        return node_formats

    def update_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        language: str = "python",
        temperature: float = 0.3,
        context_base_dir: Optional[str] = None,
        backend_port: int = 8000,
        main_output_filename: str = "main.py",
    ) -> Dict[str, Any]:
        backend_result = self.update_backend_nodes(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            node_docs_dirname=node_docs_dirname,
            language=language,
            temperature=temperature,
        )
        workflow_json_path = self.builder._main_entrypoint_service.sync_workflow_graph_json(
            context_base_dir=context_base_dir,
        )

        main_output_target = self.builder.main_output_path or os.path.join(self.builder.root_dir, main_output_filename)
        main_output_path = self.builder.generate_main_entrypoint(
            self.builder.graph_plan_path,
            output_filename=str(main_output_target),
            temperature=temperature,
            fastapi_port=backend_port,
        )

        return {
            "node_plan": backend_result["node_plan"],
            "backend_nodes": backend_result["backend_nodes"],
            "node_tests": backend_result["node_tests"],
            "workflow_json_path": workflow_json_path,
            "main_entrypoint": main_output_path,
        }

    def amend_node_markdown(
        self,
        node_name: str,
        amendment: str,
        existing_markdown_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        graph_plan_path: Optional[str] = None,
        output_path: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
        overwrite: bool = True,
    ) -> str:
        if not isinstance(node_name, str) or not node_name.strip():
            raise ValueError("node_name must be a non-empty string.")
        if not isinstance(amendment, str) or not amendment.strip():
            raise ValueError("amendment must be a non-empty string.")

        if requirement_md_path:
            self.builder.requirement_md_path = requirement_md_path
        if graph_plan_path:
            self.builder.graph_plan_path = graph_plan_path

        if not self.builder.requirement_md_path:
            raise ValueError("requirement_md_path is not set. Call analyze_requirement(...) first or pass requirement_md_path.")
        if not self.builder.graph_plan_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")

        target_markdown_path = output_path or existing_markdown_path
        if target_markdown_path is None:
            target_markdown_path = os.path.join(self.builder.root_dir, "node_docs", f"{node_name}.md")

        self.builder._logger.info("Amending node markdown '%s' -> %s", node_name, target_markdown_path)
        amended_graph_path, amended_doc_path = run_gnode_operation(
            self.builder.node_planner,
            "amend_graph_node_from_files",
            node_name=node_name,
            user_prompt=amendment,
            requirement_md_path=self.builder.requirement_md_path,
            graph_plan_json_path=self.builder.graph_plan_path,
            node_output_path=target_markdown_path,
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        self.builder.graph_plan_path = str(amended_graph_path)
        self.builder.planned_graph = self.builder._instantiate_graph(self.builder.graph_plan_path)
        run_gnode_operation(
            self.builder.planner,
            "_write_mermaid_from_graph_json",
            Path(self.builder.graph_plan_path),
        )

        final_path = str(amended_doc_path)
        self.builder.node_docs_dir = str(Path(final_path).parent)
        existing_paths = list(self.builder.node_doc_paths or [])
        expected_name = f"{node_name}.md"
        updated_paths = [path for path in existing_paths if Path(path).name != expected_name]
        updated_paths.append(final_path)
        self.builder.node_doc_paths = updated_paths
        self.builder.last_amended_node_doc_path = final_path
        return final_path


@dataclass
class MainEntrypointService:
    builder: "AgentBuilder"

    def _resolve_managed_path(self, path_value: str) -> str:
        root_dir = Path(self.builder.root_dir).expanduser()
        requested_path = Path(path_value).expanduser()

        if requested_path.is_absolute():
            return str(requested_path.resolve())

        try:
            requested_path.relative_to(root_dir)
        except ValueError:
            resolved_path = root_dir / requested_path
        else:
            resolved_path = requested_path

        if not resolved_path.is_absolute():
            resolved_path = (Path.cwd() / resolved_path).resolve()
        else:
            resolved_path = resolved_path.resolve()
        return str(resolved_path)

    def sync_workflow_graph_json(self, context_base_dir: Optional[str] = None) -> str:
        self.builder._load_planned_graph()
        target_dir = Path(self._resolve_managed_path(context_base_dir or self.builder.root_dir))
        source_path = Path(self.builder.graph_plan_path).expanduser().resolve()
        workflow_path = target_dir / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        source_text = source_path.read_text(encoding="utf-8")
        if not workflow_path.exists() or workflow_path.read_text(encoding="utf-8") != source_text:
            workflow_path.write_text(source_text, encoding="utf-8")
        self.builder.workflow_json_path = str(workflow_path)
        return self.builder.workflow_json_path

    def validate_generated_artifacts(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        doc_dir = self.builder._resolve_root_path(self.builder.node_docs_dir or node_docs_dirname)
        main_path = Path(main_entrypoint_path or self.builder.main_output_path or self.builder._resolve_root_path("main.py")).expanduser()
        if not main_path.is_absolute():
            main_path = self.builder._resolve_root_path(main_path)

        node_names = planned_graph.get_topological_sorted_nodes()
        plan_outputs: Dict[str, str] = {}
        backend_outputs: Dict[str, str] = {}
        missing: list[str] = []

        for node_name in node_names:
            doc_path = doc_dir / f"{node_name}.md"
            backend_path = self.builder._expected_backend_node_path(node_name, backend_language)
            if doc_path.is_file():
                plan_outputs[node_name] = str(doc_path)
            else:
                missing.append(str(doc_path))
            if backend_path.is_file():
                backend_outputs[node_name] = str(backend_path)
            else:
                missing.append(str(backend_path))
        if not main_path.is_file():
            missing.append(str(main_path))

        if missing:
            raise FileNotFoundError(
                "Missing artifacts required for graph refresh/server restart:\n" + "\n".join(sorted(set(missing)))
            )

        return {
            "node_plan": plan_outputs,
            "backend_nodes": backend_outputs,
            "main_entrypoint": str(main_path),
        }

    def write_main_entrypoint(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        output_filename: str = "main.py",
        fastapi_host: str = "0.0.0.0",
        temperature: float = 0.0,
        fastapi_port: int = 8000,
    ) -> str:
        selected_graph_plan_path = graph_plan_path or self.builder.graph_plan_path
        if not selected_graph_plan_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")

        self.builder.main_output_path = self._resolve_managed_path(output_filename)
        self.builder._logger.info("Generating main entrypoint -> %s", self.builder.main_output_path)
        run_gnode_operation(
            self.builder.main_writer,
            "write_main_entrypoint",
            project_root_path=self.builder.root_dir,
            graph_plan_json_path=selected_graph_plan_path,
            output_path=self.builder.main_output_path,
            requirement_analysis_result=self.builder.requirement_analysis_result,
            fastapi_host=fastapi_host,
            fastapi_port=fastapi_port,
            temperature=temperature,
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            return run_gnode_operation(
                self.builder.main_entry_auditor,
                "audit_main_entrypoint_file",
                str(self.builder.main_output_path),
                str(self.builder.root_dir),
            )

        def _retry_log(amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning(amendment)
            self.builder._logger.warning("Main entrypoint audit failed. Applying amendment...")

        def _amend(amendment: str, _audit_round: int) -> None:
            run_gnode_operation(
                self.builder.main_writer,
                "amend_code_with_feedback",
                self.builder.main_output_path,
                amendment,
                language="python",
                temperature=0.2,
            )

        repair_loop.run(
            audit=_audit,
            amend=_amend,
            failure_message_prefix=(
                "main entrypoint audit did not pass after "
                f"{repair_loop.max_attempts} attempt(s). Last feedback:\n"
            ),
            on_success=lambda _audit_round: self.builder._logger.info("Main entrypoint audit passed."),
            on_retry=_retry_log,
        )

        return self.builder.main_output_path


@dataclass
class RuntimeService:
    builder: "AgentBuilder"

    @staticmethod
    def _sanitize_node_test_log_name(node_name: str) -> str:
        safe_name = "".join(
            char if char.isalnum() or char in {"-", "_", "."} else "_"
            for char in str(node_name)
        ).strip("._")
        return safe_name or "node_test"

    def run_selected_node_tests(
        self,
        node_names: list[str],
        *,
        language: str = "python",
        log_filename: str = "node_test_log.txt",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        if not self.builder._supports_backend_node_tests(language):
            raise ValueError(
                f"single-node test execution is only supported for Python backend nodes, got: {language}"
            )

        planned_graph = self.builder._load_planned_graph()
        requested_names = {name for name in node_names if isinstance(name, str) and name.strip()}
        ordered_names = [
            name for name in planned_graph.get_topological_sorted_nodes()
            if name in requested_names
        ]
        missing_names = requested_names.difference(ordered_names)
        if missing_names:
            raise ValueError(f"node(s) not found in graph plan: {sorted(missing_names)}")

        summary_log_path = Path(os.path.join(self.builder.root_dir, log_filename))
        summary_log_path.parent.mkdir(parents=True, exist_ok=True)
        log_dir_name = summary_log_path.stem if summary_log_path.suffix else f"{summary_log_path.name}_logs"
        log_dir_path = summary_log_path.parent / log_dir_name
        log_dir_path.mkdir(parents=True, exist_ok=True)

        self.builder.log_path = str(summary_log_path)
        python_cmd = self.builder._select_python_command()
        backend_cwd = str(self.builder._resolve_backend_node_base_dir())
        cached_test_paths = self.builder.dynamic_graph_cache.get("node_tests", {})

        results: list[Dict[str, Any]] = []
        overall_ok = True

        with open(summary_log_path, "w", encoding="utf-8") as log_file:
            log_file.write("=== Node Test Run Log ===\n")
            log_file.write(f"Test started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write(f"Selected nodes: {ordered_names}\n")
            log_file.write(f"Per-node log directory: {log_dir_path}\n")
            log_file.write(f"{'=' * 50}\n\n")

            if not ordered_names:
                log_file.write("No node tests selected.\n")

            total = len(ordered_names)
            for index, node_name in enumerate(ordered_names, start=1):
                if isinstance(cached_test_paths, Mapping) and node_name in cached_test_paths:
                    resolved_test_path = self.builder._resolve_root_path(cached_test_paths[node_name])
                else:
                    resolved_test_path = self.builder._expected_backend_node_test_path(node_name, language)

                if not resolved_test_path.is_file():
                    raise FileNotFoundError(
                        f"node test file not found for node '{node_name}': {resolved_test_path}"
                    )

                command = [python_cmd, "-m", "pytest", str(resolved_test_path), "-q"]
                self.builder._logger.info(
                    "[%s/%s] Running node test '%s' -> %s",
                    index,
                    total,
                    node_name,
                    resolved_test_path,
                )

                node_log_path = log_dir_path / f"{self._sanitize_node_test_log_name(node_name)}.txt"
                log_file.write(f"[{index}/{total}] Node test: {node_name}\n")
                log_file.write(f"Test file: {resolved_test_path}\n")
                log_file.write(f"Log file: {node_log_path}\n")

                run_node_test = RunNodeTest(
                    node_name=node_name,
                    test_path=resolved_test_path,
                    log_path=node_log_path,
                    command=command,
                    cwd=backend_cwd,
                    timeout=timeout,
                    run_subprocess=self.builder._run_subprocess,
                    timeout_expired=self.builder._subprocess_timeout_expired(),
                )
                status = run_node_test.run()
                if status.isErr() or run_node_test.result is None:
                    raise RuntimeError(
                        f"node test runner failed for {node_name}: {status.getInfo()}"
                    )

                result_item = run_node_test.result
                overall_ok = overall_ok and result_item["ok"]
                log_file.write(f"Status: {result_item['status_line']}\n\n")
                results.append(result_item)

            log_file.write(f"{'=' * 50}\n")
            log_file.write(f"Test ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        self.builder._logger.info("Node test summary log written to: %s", self.builder.log_path)
        self.builder._logger.info("Per-node test logs written to: %s", log_dir_path)
        self.builder._logger.debug(
            "Node test run completed for nodes=%s ok=%s",
            ordered_names,
            overall_ok,
        )
        return {
            "ok": overall_ok,
            "log_path": self.builder.log_path,
            "log_dir": str(log_dir_path),
            "log_paths": {
                result_item["node_name"]: result_item["log_path"]
                for result_item in results
            },
            "results": results,
        }

    def stop_managed_server_process(self, process: Optional[Any]) -> None:
        if process is None:
            return
        try:
            if callable(getattr(process, "poll", None)) and process.poll() is not None:
                return
        except Exception:
            return

        try:
            process.terminate()
            if callable(getattr(process, "wait", None)):
                process.wait(timeout=5)
            return
        except Exception:
            pass

        try:
            process.kill()
            if callable(getattr(process, "wait", None)):
                process.wait(timeout=5)
        except Exception:
            return

    def rerun_server(
        self,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
        backend_port: int = 8000,
    ) -> Dict[str, Any]:
        del backend_port
        if graph_plan_path:
            self.builder.graph_plan_path = graph_plan_path
        if not self.builder.graph_plan_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")

        artifact_state = self.builder._main_entrypoint_service.validate_generated_artifacts(
            graph_plan_path=self.builder.graph_plan_path,
            node_docs_dirname=node_docs_dirname,
            backend_language=backend_language,
            main_entrypoint_path=main_entrypoint_path,
        )

        main_path = Path(artifact_state["main_entrypoint"]).expanduser().resolve()

        self.stop_managed_server_process(self.builder.backend_server_process)

        python_cmd = self.builder._select_python_command()
        backend_command = [python_cmd, str(main_path)]

        self.builder.backend_server_process = self.builder._popen_subprocess(
            backend_command,
            cwd=str(main_path.parent),
            env=os.environ.copy(),
        )

        server_runtime = {
            "backend": {
                "pid": getattr(self.builder.backend_server_process, "pid", None),
                "command": backend_command,
                "cwd": str(main_path.parent),
            },
            "artifacts": artifact_state,
        }
        self.builder.dynamic_graph_cache["server_runtime"] = server_runtime
        return server_runtime

    def run_node_tests(
        self,
        graph_plan_path: Optional[str] = None,
        node_names: Optional[list[str]] = None,
        backend_language: str = "python",
        log_filename: str = "node_test_log.txt",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        selected_node_names = node_names
        if selected_node_names is None:
            selected_node_names = planned_graph.get_topological_sorted_nodes()

        return self.run_selected_node_tests(
            selected_node_names,
            language=backend_language,
            log_filename=log_filename,
            timeout=timeout,
        )

    def test_main_entrypoint(
        self,
        main_entrypoint_path: str,
        log_filename: str = "test_log.txt",
        graph_plan_path: Optional[str] = None,
    ) -> bool:
        del graph_plan_path
        abs_path = os.path.abspath(main_entrypoint_path)
        self.builder.log_path = os.path.join(self.builder.root_dir, log_filename)

        with open(self.builder.log_path, "w") as log_file:
            log_file.write("=== Main Entrypoint Test Log ===\n")
            log_file.write(f"Test started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            log_file.write(f"Testing file: {abs_path}\n")
            log_file.write(f"{'=' * 50}\n\n")

            try:
                python_cmd = self.builder._select_python_command()
                log_file.write(f"Executing: {python_cmd} {abs_path}\n\n")
                result = self.builder._run_subprocess(
                    [python_cmd, abs_path],
                    cwd=os.path.dirname(abs_path),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

                log_file.write(f"Return code: {result.returncode}\n\n")

                if result.stdout:
                    log_file.write(f"--- STDOUT ---\n{result.stdout}\n\n")
                if result.stderr:
                    log_file.write(f"--- STDERR ---\n{result.stderr}\n\n")

                if result.returncode == 0:
                    log_file.write("✓ Test completed successfully.\n")
                else:
                    log_file.write(f"✗ Test failed with return code {result.returncode}.\n")

            except self.builder._subprocess_timeout_expired():
                log_file.write("✗ Test timed out after 60 seconds.\n")
            except Exception as exc:
                log_file.write(f"✗ Test raised an exception: {type(exc).__name__}: {exc}\n")

            log_file.write(f"\n{'=' * 50}\n")
            log_file.write(f"Test ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

        self.builder._logger.info("Test log written to: %s", self.builder.log_path)
        self.builder._logger.debug("Main entrypoint test log generated for %s", abs_path)

        return self.amend_by_log(self.builder.log_path)

    def amend_by_log(self, log_path: str) -> bool:
        ok, violations = run_gnode_operation(self.builder.output_auditor, "audit_log_file", log_path)
        if not ok:
            for violation in violations:
                fname = violation.rule
                detail = violation.detail
                coder, target_path = self.builder._resolve_amendment_target(fname, language="python")

                if coder is None:
                    self.builder._logger.warning("Skipping amendment for unresolved target: %s", fname)
                    continue

                try:
                    self.builder._logger.warning("Applying amendment to %s: %s", fname, detail)
                    current_node_name = Path(target_path).stem
                    run_gnode_operation(
                        coder,
                        "amend_code_with_feedback",
                        target_path,
                        detail,
                        graph_plan_path=self.builder.graph_plan_path or "",
                        requirement_md_path=self.builder.requirement_md_path or "",
                        current_node_name=current_node_name,
                        language="python",
                        temperature=0.3,
                    )
                except Exception as exc:
                    self.builder._logger.error("Failed to amend %s: %s", fname, exc, exc_info=True)
        return ok


def build_steps_meta(builder: "AgentBuilder", include_hidden_nodes: bool = False) -> list[dict[str, Any]]:
    planned_graph = builder._load_planned_graph()
    steps_meta: list[dict[str, Any]] = []

    for node_name in planned_graph.get_topological_sorted_nodes():
        node_meta = planned_graph.get_node_meta(node_name)
        if node_meta is None:
            continue
        if not include_hidden_nodes and not bool(getattr(node_meta, "enable", True)):
            continue

        reference = resolve_workflow_node_reference(
            meta_node_kind=getattr(node_meta, "meta_node_kind", None),
            ext_data=getattr(node_meta, "ext_data", None),
        )
        prompt = str(getattr(node_meta, "desc", "") or "").strip() or reference.summary
        ext_data = getattr(node_meta, "ext_data", None)

        steps_meta.append(
            {
                "id": node_name,
                "title": node_name,
                "prompt": prompt,
                "dependencies": list(getattr(node_meta, "depends", []) or []),
                "inputRequired": bool(reference.input_required),
                "nodeKind": str(reference.capability_category or "operation"),
                "extData": ext_data if isinstance(ext_data, Mapping) else (ext_data or {}),
                "metaNodeKind": str(reference.meta_node_kind or ""),
                "enabled": bool(getattr(node_meta, "enable", True)),
            }
        )

    return steps_meta