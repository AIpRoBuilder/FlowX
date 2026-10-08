from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, TYPE_CHECKING

from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.tools.agent_builder_tools import get_language_extension
from flowx_core.tools.workflow_node_reference import resolve_workflow_node_reference
from flowx_core.workflows.node_generation import (
    NodeGenerationContext,
    NodeGenerationWorkflow,
    WorkflowCallback,
)


if TYPE_CHECKING:
    from flowx_core.agent_builder import AgentBuilder


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
            self.builder._logger.info("Using existing requirement file -> %s", requirement_file)
            return requirement_file

        out_path = os.path.join(self.builder.root_dir, out_file)
        self.builder._logger.info("Analyzing requirement -> %s", out_path)
        self.builder._logger.debug(
            "Requirement analysis input length=%s output_file=%s",
            len(requirement_text or ""),
            out_path,
        )
        result = self.builder.analyzer.process_input(
            requirement_text or "",
            {},
            {"RequirementDisector::action": "write", "requirement_disector": {
                "output_path": out_path,
            }},
        )
        self.builder.requirement_md_path = result.derived["requirement_md_path"]
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
        self.builder.planner.process_input(
            "",
            {},
            {"GraphPlanner::action": "write", "graph_planner": {
                "requirement_md_path": self.builder.requirement_md_path,
                "output_path": self.builder.graph_plan_path,
            }},
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            self.builder.planned_graph = self.builder._instantiate_graph(self.builder.graph_plan_path)
            return self.builder.graph_auditor.audit_graph_json(self.builder.planned_graph)

        def _retry_log(amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning("Graph audit failed. Applying amendment %s...", amendment)

        def _amend(amendment: str, _audit_round: int) -> None:
            self.builder.planner.process_input(
                "",
                {},
                {"GraphPlanner::action": "amend", "graph_planner": {
                    "graph_json_path": self.builder.graph_plan_path,
                    "amendment": amendment,
                    "temperature": temperature,
                }},
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
        self.builder.planner._write_mermaid_from_graph_json(Path(self.builder.graph_plan_path))
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

        self.builder.planner.process_input(
            "",
            {},
            {"GraphPlanner::action": "amend", "graph_planner": {
                "graph_json_path": self.builder.graph_plan_path,
                "amendment": amendment,
                "temperature": temperature,
            }},
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            self.builder.planned_graph = self.builder._instantiate_graph(self.builder.graph_plan_path)
            return self.builder.graph_auditor.audit_graph_json(self.builder.planned_graph)

        def _retry_log(_amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning("Graph amendment audit failed. Applying amendment...")

        def _amend(next_amendment: str, _audit_round: int) -> None:
            self.builder.planner.process_input(
                "",
                {},
                {"GraphPlanner::action": "amend", "graph_planner": {
                    "graph_json_path": self.builder.graph_plan_path,
                    "amendment": next_amendment,
                    "temperature": temperature,
                }},
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

        self.builder.planner._write_mermaid_from_graph_json(Path(self.builder.graph_plan_path))
        return self.builder.graph_plan_path


@dataclass
class NodeBuildService:
    """Run node generation per request, using one single-node workflow per meta_node_kind."""

    builder: "AgentBuilder"
    workflows: Dict[str, NodeGenerationWorkflow] = field(default_factory=dict)  # keyed by meta_node_kind
    contexts: Dict[str, NodeGenerationContext] = field(default_factory=dict)  # keyed by workflow_name

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

    @staticmethod
    def _expected_node_path(graph_plan_path: str, node_name: str, language: str) -> Path:
        return Path(graph_plan_path).parent / f"{node_name}{get_language_extension(language)}"

    def _node_path(self, graph_plan_path: str, node_name: str, language: str) -> Path:
        location = self.builder.node_location_map.get(node_name)
        if location:
            return self.builder._resolve_root_path(location)
        return self._expected_node_path(graph_plan_path, node_name, language)

    def _seed_existing_artifacts(
        self,
        context: NodeGenerationContext,
        ordered_names: list[str],
    ) -> None:
        for name in ordered_names:
            markdown_path = context.node_docs_dir / f"{name}.md"
            node_path = self._node_path(context.graph_plan_path, name, context.language)
            if markdown_path.is_file() or node_path.is_file():
                context.artifacts[name] = {
                    "node_name": name,
                    "node_file_path": str(node_path) if node_path.is_file() else "",
                    "node_markdown_path": str(markdown_path) if markdown_path.is_file() else "",
                    "node_file": node_path.read_text(encoding="utf-8") if node_path.is_file() else "",
                    "node_markdown": markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else "",
                    "node_formats": dict(context.node_formats.get(name, {})),
                }

    def _collect_node_formats(self, graph: Graph, language: str) -> dict[str, dict[str, Any]]:
        from flowx_core.tools.file_tools import compile_node_file_and_get_step_output_card_schema

        formats: dict[str, dict[str, Any]] = {}
        for name in graph.get_topological_sorted_nodes():
            meta = graph.get_node_meta(name)
            ext_data = meta.ext_data or {}
            ext_type = str(ext_data.get("type", "none") if isinstance(ext_data, Mapping) else ext_data).strip().lower()
            inputs = meta.inputs_format or {}
            path = self._node_path(str(graph.graph_json_path), name, language)
            schema = compile_node_file_and_get_step_output_card_schema(str(path)) if path.is_file() else None
            formats[name] = {
                "user_input_format": {
                    str(key).strip(): str(value).strip().lower()
                    for key, value in inputs.items() if str(key).strip() and str(value).strip()
                } if ext_type in {"user_input", "skill"} else {},
                "backend_output_card_format": schema.get("card") if schema else None,
                "backend_node_path": str(path) if path.is_file() else None,
            }
        return formats

    def _run_requested_workflows(self, context: NodeGenerationContext, node_name: str, artifact: dict[str, Any]) -> dict[str, Any]:
        request = context.workflow_requests.get(node_name)
        if request is None or request is False:
            return {}
        if request is True:
            names = list(context.workflow_registry)
        elif isinstance(request, str):
            names = [request]
        elif isinstance(request, Sequence) and not isinstance(request, (str, bytes)):
            names = [str(item) for item in request]
        else:
            raise TypeError(f"workflow request for {node_name!r} must be a bool, name, or sequence of names")
        unknown = set(names).difference(context.workflow_registry)
        if unknown:
            raise ValueError(f"unknown workflow(s) requested for {node_name}: {sorted(unknown)}")
        return {
            name: context.workflow_registry[name](
                node_name=node_name, artifact=dict(artifact), context=context,
            )
            for name in names
        }

    def _publish_node(self, context: NodeGenerationContext, node_name: str, generated_path: str) -> None:
        markdown_file = context.node_docs_dir / f"{node_name}.md"
        markdown_path = str(markdown_file.resolve()) if markdown_file.is_file() else None
        artifact: dict[str, Any] = {
            "node_name": node_name,
            "node_file_path": generated_path,
            "node_markdown_path": markdown_path,
            "node_file": Path(generated_path).read_text(encoding="utf-8") if Path(generated_path).is_file() else "",
            "node_markdown": markdown_file.read_text(encoding="utf-8") if markdown_path else "",
            "node_formats": dict(context.node_formats.get(node_name, {})),
        }
        artifact["workflow_outputs"] = self._run_requested_workflows(context, node_name, artifact)
        self.builder.node_location_map[node_name] = generated_path
        context.artifacts[node_name] = artifact
        if markdown_path:
            self.builder.node_docs_dir = str(Path(markdown_path).parent)
            self.builder.node_doc_paths = [
                path for path in (self.builder.node_doc_paths or [])
                if Path(path).name != Path(markdown_path).name
            ] + [markdown_path]
        self.builder._advance_progress(f"Generated node: {node_name}")

    def _workflow_for(self, node_meta: NodeMeta) -> NodeGenerationWorkflow:
        """Pick the coder subclass from meta_node_kind/ext_data via the factory and store one workflow per kind."""

        meta_node_kind = resolve_workflow_node_reference(
            meta_node_kind=node_meta.meta_node_kind or None,
            ext_data=node_meta.ext_data,
        ).meta_node_kind
        workflow = self.workflows.get(meta_node_kind)
        if workflow is None:
            workflow = self.workflows[meta_node_kind] = NodeGenerationWorkflow(
                meta_node_kind=meta_node_kind,
                node_planner=self.builder.node_planner,
                node_coder=self.builder._make_node_coder(node_meta),
                node_auditor=self.builder.node_auditor,
            )
        workflow.node_planner = self.builder.node_planner
        workflow.node_auditor = self.builder.node_auditor
        return workflow

    def run(
        self,
        *,
        workflow_name: str = "default",
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
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        workflow_name = workflow_name.strip()
        selected_graph_path = Path(graph_plan_path or self.builder.graph_plan_path or "").expanduser().resolve()
        planned_graph = Graph(str(selected_graph_path))
        graph_payload = json.loads(selected_graph_path.read_text(encoding="utf-8"))
        raw_nodes = self._raw_node_map(graph_payload)
        all_names = planned_graph.get_topological_sorted_nodes()
        ordered_names = self._normalize_requested_names(all_names, node_names, run_nodes)

        requirement_path = Path(requirement_md_path or self.builder.requirement_md_path or "").expanduser().resolve()
        requirement_text = requirement_path.read_text(encoding="utf-8")
        if reset_mappings:
            self.builder.node_location_map.clear()

        docs_dir = self.builder._resolve_root_path(node_docs_dirname)
        docs_dir.mkdir(parents=True, exist_ok=True)
        try:
            formats = self._collect_node_formats(planned_graph, language)
        except Exception:
            formats = {}

        context = NodeGenerationContext(
            graph_plan_path=str(selected_graph_path),
            requirement_md_path=str(requirement_path),
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
        self.contexts[workflow_name] = context
        self._seed_existing_artifacts(context, context.skipped_node_names)
        if not ordered_names:
            return context

        self.builder._start_progress(len(ordered_names))
        for node_name in ordered_names:
            node_meta = planned_graph.get_node_meta(node_name)
            target_path = self._node_path(context.graph_plan_path, node_name, language)
            generated_path = self._workflow_for(node_meta).run(context, node_meta, node_name, target_path)
            self._publish_node(context, node_name, generated_path)
        return context


@dataclass
class NodeArtifactService:
    """Create and run test-driven modifiers keyed by workflow and node name."""

    builder: "AgentBuilder"
    modifiers: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def get_or_create_modifier(self, workflow_name: str, node_name: str) -> Any:
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        if not isinstance(node_name, str) or not node_name.strip():
            raise ValueError("node_name must be a non-empty string")

        workflow_name = workflow_name.strip()
        node_name = node_name.strip()
        workflow_modifiers = self.modifiers.setdefault(workflow_name, {})
        modifier = workflow_modifiers.get(node_name)
        if modifier is None:
            from flowx_core.workflows.node_runtime_modifier import (
                AmendNodeFromTestLog,
                GenerateNodeTestFile,
                NodeRuntimeModifierPipeline,
            )

            root_dir_path = str(Path(self.builder.root_dir).expanduser().resolve())
            node_test_writer = GenerateNodeTestFile(
                api_key=self.builder.api_key,
                model=self.builder.model,
                provider=self.builder.provider,
                root_dir_path=root_dir_path,
                session_marking_prompt=self.builder.session_marking_prompt,
            )
            node_writer = AmendNodeFromTestLog(
                api_key=self.builder.api_key,
                model=self.builder.model,
                provider=self.builder.provider,
                session_marking_prompt=self.builder.session_marking_prompt,
            )
            modifier = NodeRuntimeModifierPipeline(
                node_test_writer=node_test_writer,
                node_writer=node_writer,
                run_subprocess=self.builder._run_subprocess,
                timeout_expired=self.builder._subprocess_timeout_expired(),
            )
            workflow_modifiers[node_name] = modifier
        return modifier

    def run_modifier(
        self,
        *,
        workflow_name: str,
        node_name: str,
        graph_plan_path: Optional[str] = None,
        generate_test: bool = True,
        run_test: bool = True,
        amend_node: bool = True,
        timeout: int = 60,
        temperature: float = 0.2,
        amendment_temperature: float = 0.3,
    ) -> Any:
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        if not isinstance(node_name, str) or not node_name.strip():
            raise ValueError("node_name must be a non-empty string")
        workflow_name = workflow_name.strip()
        node_name = node_name.strip()
        generation_context = self.builder._node_build_service.contexts.get(workflow_name)
        if graph_plan_path is None and workflow_name != "default":
            if generation_context is None:
                raise ValueError(
                    f"graph_plan_path is required for workflow {workflow_name!r} before it has been generated"
                )
            graph_plan_path = generation_context.graph_plan_path
        planned_graph = self.builder._load_planned_graph(graph_plan_path)
        if node_name not in planned_graph.get_topological_sorted_nodes():
            raise ValueError(f"node {node_name!r} not found in workflow {workflow_name!r}")
        workflow_path = Path(self.builder.graph_plan_path).expanduser().resolve()
        node_path_value = None
        if generation_context is not None and Path(generation_context.graph_plan_path).resolve() == workflow_path:
            node_path_value = generation_context.artifacts.get(node_name, {}).get("node_file_path")
        if not node_path_value:
            mapped_path = self.builder.node_location_map.get(node_name)
            if mapped_path and self.builder._resolve_root_path(mapped_path).parent == workflow_path.parent:
                node_path_value = mapped_path
        node_path = (
            self.builder._resolve_root_path(node_path_value)
            if node_path_value
            else workflow_path.parent / f"{node_name}.py"
        )
        if not node_path.is_file():
            raise FileNotFoundError(
                f"backend node file not found for workflow {workflow_name!r}, node {node_name!r}: {node_path}"
            )

        modifier = self.get_or_create_modifier(workflow_name, node_name)
        root_dir_path = str(workflow_path.parent)
        modifier.node_test_writer.root_dir_path = root_dir_path
        workflow_tests = self.builder.dynamic_graph_cache.get("workflow_node_tests", {})
        test_paths = workflow_tests.get(workflow_name, {}) if isinstance(workflow_tests, Mapping) else {}
        if workflow_name == "default" and not test_paths:
            test_paths = self.builder.dynamic_graph_cache.get("node_tests", {})
        cached_test_path = test_paths.get(node_name) if isinstance(test_paths, Mapping) else None
        workflow_component = "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in workflow_name
        ).strip("._") or "workflow"
        default_test_name = (
            str(Path("tests") / workflow_component / f"test_{node_name}.py")
            if workflow_name != "default" else ""
        )
        context = modifier.run(
            node_file_name=str(node_path),
            workflow_graph=str(workflow_path),
            working_directory=str(workflow_path.parent),
            python_command=self.builder._select_python_command() if run_test else "",
            timeout=timeout,
            test_temperature=temperature,
            amendment_temperature=amendment_temperature,
            workflow_name=workflow_name,
            node_name=node_name,
            test_file_name=str(cached_test_path) if not generate_test and cached_test_path else default_test_name,
            log_file_name=(
                str(Path("logs") / workflow_component / f"{node_name}_test.log")
                if workflow_name != "default"
                else ""
            ),
            skip_generate_node_test=not generate_test,
            skip_run_node_test=not run_test,
            skip_amend_node=not amend_node,
        )
        if context.test_file_path:
            workflow_maps = self.builder.dynamic_graph_cache.get("workflow_node_tests", {})
            workflow_maps = dict(workflow_maps) if isinstance(workflow_maps, Mapping) else {}
            test_map = workflow_maps.get(workflow_name, {})
            test_map = dict(test_map) if isinstance(test_map, Mapping) else {}
            test_map[node_name] = context.test_file_path
            workflow_maps[workflow_name] = test_map
            self.builder.dynamic_graph_cache["workflow_node_tests"] = workflow_maps
            if workflow_name == "default":
                self.builder.dynamic_graph_cache["node_tests"] = dict(test_map)
        if context.amended_node_file_path:
            self.builder.node_location_map[node_name] = context.amended_node_file_path
        return context


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
        self.builder.main_writer.process_input(
            "",
            {},
            {"PromptMainFileCoder::action": "write", "main_entrypoint": {
                "project_root_path": self.builder.root_dir,
                "graph_plan_json_path": selected_graph_plan_path,
                "output_path": self.builder.main_output_path,
                "fastapi_host": fastapi_host,
                "fastapi_port": fastapi_port,
                "temperature": temperature,
            }},
        )

        repair_loop = self.builder._make_audit_repair_loop()

        def _audit() -> tuple[bool, list[Any]]:
            return self.builder.main_entry_auditor.audit_main_entrypoint_file(
                str(self.builder.main_output_path),
                str(self.builder.root_dir),
            )

        def _retry_log(amendment: str, _audit_round: int) -> None:
            self.builder._logger.warning(amendment)
            self.builder._logger.warning("Main entrypoint audit failed. Applying amendment...")

        def _amend(amendment: str, _audit_round: int) -> None:
            self.builder.main_writer.process_input(
                "",
                {},
                {"PromptMainFileCoder::action": "amend", "main_entrypoint": {
                    "output_path": self.builder.main_output_path,
                    "amendment": amendment,
                    "language": "python",
                    "temperature": 0.2,
                }},
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
        ok, violations = self.builder.output_auditor.audit_log_file(log_path)
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
                    coder.amend_code_with_feedback(
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