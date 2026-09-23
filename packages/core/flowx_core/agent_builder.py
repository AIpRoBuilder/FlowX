from typing import Optional, List, Dict, Any, Mapping
import os
import subprocess
import json
from pathlib import Path

from flowx_core.architect import GraphPlanner, NodePlanner, Graph
from flowx_core.auditor import GraphJsonAuditor, NodeAuditor, MainEntryPointAuditor, OutputAuditor
from flowx_core.builder_support import AuditRepairLoop, BuildSession, BuilderComponentFactory
from flowx_core.builder_services import (
    GraphBuildService,
    MainEntrypointService,
    NodeBuildService,
    RuntimeService,
    build_steps_meta,
)
from flowx_core.llm_client.coder import MAX_TOKENS, compose_session_marking_prompt
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import (
    PromptNodeFileCoderBase,
    SpatialTemporalContractNodeCoder,
    WorkflowOperationNodeCoder,
    WorkflowSkillNodeCoder,
    WorkflowFileNodeCoder,
    WorkflowStepNodeCoder,
)
from flowx_core.demand_analyzer import RequirementDisector
from flowx_core.logging_utils import configure_runtime_logging, get_logger
from flowx_core.tools.agent_builder_tools import (
    get_language_extension,
    select_python_command,
)


DEFAULT_MAX_AUDIT_ROUNDS = 7


class AgentBuilder:
    """Build AG-UI workflow artifacts with generation, auditing, and progress display."""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        provider: str = "deepseek",
        root_dir: str = "./example",
        skills_root_path: Optional[str] = None,
        log_level: str | int | None = None,
        log_filename: str = "flowx_core_debug.log",
        max_audit_rounds: int = DEFAULT_MAX_AUDIT_ROUNDS,
        session_marking_prompt: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.root_dir = root_dir
        self.skills_root_path = skills_root_path.strip() if isinstance(skills_root_path, str) else ""
        self.max_audit_rounds = self._validate_max_audit_rounds(max_audit_rounds)
        self.session_marking_prompt = compose_session_marking_prompt(session_marking_prompt)
        self._session = BuildSession()
        os.makedirs(self.root_dir, exist_ok=True)
        self.log_level = log_level or os.getenv("META_AGENT_LOG_LEVEL", "INFO")
        self.runtime_log_path = str(
            configure_runtime_logging(
                self.root_dir,
                log_filename=log_filename,
                console_level=self.log_level,
            )
        )
        self._logger = get_logger(__name__)
        self._logger.info(f"Runtime log file: {self.runtime_log_path}")
        self._logger.debug(
            "Initialized AgentBuilder with model=%s provider=%s root_dir=%s skills_root_path=%s max_audit_rounds=%s",
            self.model,
            self.provider,
            Path(self.root_dir).expanduser().resolve(),
            self.skills_root_path,
            self.max_audit_rounds,
        )

        self._progress_total = 0
        self._progress_current = 0
        self._progress_width = 28

        self._reset_llm_components()
        # initialize auditors for later validation steps
        self.graph_auditor = GraphJsonAuditor()
        self.node_auditor = NodeAuditor()
        self.main_entry_auditor = MainEntryPointAuditor()
        # output auditor will inspect test logs and help trigger amendments
        self.output_auditor = OutputAuditor()
        self._graph_build_service = GraphBuildService(self)
        self._node_build_service = NodeBuildService(self)
        self._main_entrypoint_service = MainEntrypointService(self)
        self._runtime_service = RuntimeService(self)

    @property
    def artifact_state(self) -> Any:
        return self._session.artifacts

    @property
    def runtime_state(self) -> Any:
        return self._session.runtime

    @property
    def requirement_md_path(self) -> Optional[str]:
        return self._session.requirement_md_path

    @requirement_md_path.setter
    def requirement_md_path(self, value: Optional[str]) -> None:
        self._session.requirement_md_path = value

    @property
    def requirement_analysis_result(self) -> Optional[Dict[str, Any]]:
        return self._session.requirement_analysis_result

    @requirement_analysis_result.setter
    def requirement_analysis_result(self, value: Optional[Dict[str, Any]]) -> None:
        self._session.requirement_analysis_result = value

    @property
    def graph_plan_path(self) -> Optional[str]:
        return self._session.graph_plan_path

    @graph_plan_path.setter
    def graph_plan_path(self, value: Optional[str]) -> None:
        self._session.graph_plan_path = value

    @property
    def planned_graph(self) -> Any:
        return self._session.planned_graph

    @planned_graph.setter
    def planned_graph(self, value: Any) -> None:
        self._session.planned_graph = value

    @property
    def workflow_json_path(self) -> Optional[str]:
        return self._session.workflow_json_path

    @workflow_json_path.setter
    def workflow_json_path(self, value: Optional[str]) -> None:
        self._session.workflow_json_path = value

    @property
    def main_output_path(self) -> Optional[str]:
        return self._session.main_output_path

    @main_output_path.setter
    def main_output_path(self, value: Optional[str]) -> None:
        self._session.main_output_path = value

    @property
    def node_docs_dir(self) -> Optional[str]:
        return self._session.node_docs_dir

    @node_docs_dir.setter
    def node_docs_dir(self, value: Optional[str]) -> None:
        self._session.node_docs_dir = value

    @property
    def node_doc_paths(self) -> List[str]:
        return self._session.node_doc_paths

    @node_doc_paths.setter
    def node_doc_paths(self, value: List[str]) -> None:
        self._session.node_doc_paths = value

    @property
    def last_amended_node_doc_path(self) -> Optional[str]:
        return self._session.last_amended_node_doc_path

    @last_amended_node_doc_path.setter
    def last_amended_node_doc_path(self, value: Optional[str]) -> None:
        self._session.last_amended_node_doc_path = value

    @property
    def log_path(self) -> Optional[str]:
        return self._session.log_path

    @log_path.setter
    def log_path(self, value: Optional[str]) -> None:
        self._session.log_path = value

    @property
    def node_location_map(self) -> Dict[str, str]:
        return self._session.node_location_map

    @node_location_map.setter
    def node_location_map(self, value: Dict[str, str]) -> None:
        self._session.node_location_map = value

    @property
    def node_coder_map(self) -> Dict[str, Any]:
        return self._session.node_coder_map

    @node_coder_map.setter
    def node_coder_map(self, value: Dict[str, Any]) -> None:
        self._session.node_coder_map = value

    @property
    def dynamic_graph_cache(self) -> Dict[str, Any]:
        return self._session.dynamic_graph_cache

    @dynamic_graph_cache.setter
    def dynamic_graph_cache(self, value: Dict[str, Any]) -> None:
        self._session.dynamic_graph_cache = value

    @property
    def backend_server_process(self) -> Any:
        return self._session.backend_server_process

    @backend_server_process.setter
    def backend_server_process(self, value: Any) -> None:
        self._session.backend_server_process = value

    @staticmethod
    def _validate_max_audit_rounds(max_audit_rounds: int) -> int:
        if max_audit_rounds < 1:
            raise ValueError("max_audit_rounds must be at least 1.")
        return max_audit_rounds

    def _resolve_max_audit_rounds(self, max_audit_rounds: Optional[int] = None) -> int:
        if max_audit_rounds is None:
            return self.max_audit_rounds
        return self._validate_max_audit_rounds(max_audit_rounds)

    def _make_audit_repair_loop(self, max_attempts: Optional[int] = None) -> AuditRepairLoop:
        return AuditRepairLoop(
            logger=self._logger,
            max_attempts=self._resolve_max_audit_rounds(max_attempts),
        )

    def _select_python_command(self) -> str:
        return select_python_command()

    def _instantiate_graph(self, graph_plan_path: str) -> Any:
        return Graph(graph_plan_path)

    def _popen_subprocess(self, *args: Any, **kwargs: Any) -> Any:
        return subprocess.Popen(*args, **kwargs)

    def _run_subprocess(self, *args: Any, **kwargs: Any) -> Any:
        return subprocess.run(*args, **kwargs)

    def _subprocess_timeout_expired(self) -> type[subprocess.TimeoutExpired]:
        return subprocess.TimeoutExpired

    def _build_component_factory(self) -> BuilderComponentFactory:
        return BuilderComponentFactory(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            root_dir=self.root_dir,
            skills_root_path=self.skills_root_path,
            session_marking_prompt=self.session_marking_prompt,
            analyzer_cls=RequirementDisector,
            planner_cls=GraphPlanner,
            node_planner_cls=NodePlanner,
            main_writer_cls=PromptMainFileCoder,
            workflow_step_node_coder_cls=WorkflowStepNodeCoder,
            workflow_operation_node_coder_cls=WorkflowOperationNodeCoder,
            workflow_file_node_coder_cls=WorkflowFileNodeCoder,
            workflow_skill_node_coder_cls=WorkflowSkillNodeCoder,
            spatial_temporal_contract_node_coder_cls=SpatialTemporalContractNodeCoder,
        )

    def _reset_llm_components(self) -> None:
        self._logger.debug(
            "Resetting LLM-backed components for model=%s provider=%s",
            self.model,
            self.provider,
        )
        self._component_factory = self._build_component_factory()
        bundle = self._component_factory.create_bundle()
        self.analyzer = bundle.analyzer
        self.planner = bundle.planner
        self.node_planner = bundle.node_planner
        self.main_writer = bundle.main_writer

    def reset_llm_config(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        if api_key is not None:
            self.api_key = api_key
        if model is not None:
            self.model = model
        if provider is not None:
            self.provider = provider
        self._logger.info("Resetting LLM configuration.")
        self._reset_llm_components()

    def _make_node_coder(self, node_meta: Any) -> PromptNodeFileCoderBase:
        return self._component_factory.create_node_coder(
            node_meta,
            root_dir_path=self.root_dir,
        )

    def _make_node_test_coder(self) -> PromptNodeTestFileCoder:
        return PromptNodeTestFileCoder(
            api_key=self.api_key,
            model=self.model,
            provider=self.provider,
            root_dir_path=str(self._resolve_backend_node_base_dir()),
            session_marking_prompt=self.session_marking_prompt,
        )

    def _resolve_root_path(self, path_value: str | Path) -> Path:
        resolved_path = Path(path_value).expanduser()
        if not resolved_path.is_absolute():
            resolved_path = Path(self.root_dir).expanduser() / resolved_path
        return resolved_path.resolve()

    def _resolve_backend_node_base_dir(self) -> Path:
        if self.graph_plan_path:
            return Path(self.graph_plan_path).expanduser().resolve().parent
        return Path(self.root_dir).expanduser().resolve()

    def _sync_node_coder_root_dir(self, coder: PromptNodeFileCoderBase) -> None:
        if hasattr(coder, "root_dir_path"):
            coder.root_dir_path = str(self._resolve_backend_node_base_dir())

    def _load_planned_graph(self, graph_plan_path: Optional[str] = None) -> Graph:
        if graph_plan_path:
            self.graph_plan_path = graph_plan_path
        if not self.graph_plan_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")

        graph_path = Path(self.graph_plan_path).expanduser().resolve()
        planned_graph = getattr(self, "planned_graph", None)
        planned_graph_path: Optional[Path] = None
        if planned_graph is not None and hasattr(planned_graph, "graph_json_path"):
            try:
                planned_graph_path = Path(planned_graph.graph_json_path).resolve()
            except Exception:
                planned_graph_path = None
        if planned_graph is None or planned_graph_path != graph_path:
            self.planned_graph = Graph(str(graph_path))
        return self.planned_graph

    def _read_requirement_text(self, requirement_md_path: Optional[str] = None) -> str:
        if requirement_md_path:
            self.requirement_md_path = requirement_md_path
        if not self.requirement_md_path:
            raise ValueError("requirement_md_path is not set. Call analyze_requirement(...) first or pass requirement_md_path.")
        requirement_path = Path(self.requirement_md_path).expanduser().resolve()
        return requirement_path.read_text(encoding="utf-8")

    def _read_graph_plan_payload(self) -> Dict[str, Any]:
        self._load_planned_graph()
        graph_path = Path(self.graph_plan_path).expanduser().resolve()
        return json.loads(graph_path.read_text(encoding="utf-8"))

    def _build_filtered_graph_plan_payload(self, node_names: List[str]) -> Dict[str, Any]:
        graph_payload = self._read_graph_plan_payload()
        requested_names = {name for name in node_names if isinstance(name, str) and name.strip()}
        nodes = graph_payload.get("nodes", []) if isinstance(graph_payload, dict) else []
        if not isinstance(nodes, list):
            raise ValueError("graph_plan JSON must contain a top-level 'nodes' list.")
        graph_payload["nodes"] = [
            node for node in nodes
            if isinstance(node, Mapping) and str(node.get("name", "")).strip() in requested_names
        ]
        return graph_payload

    def _sync_workflow_graph_json(self, context_base_dir: Optional[str] = None) -> str:
        return self._main_entrypoint_service.sync_workflow_graph_json(
            context_base_dir=context_base_dir,
        )

    def _expected_backend_node_path(self, node_name: str, language: str = "python") -> Path:
        return (self._resolve_backend_node_base_dir() / f"{node_name}{get_language_extension(language)}").resolve()

    @staticmethod
    def _supports_backend_node_tests(language: str = "python") -> bool:
        return get_language_extension(language) == ".py"

    def _expected_backend_node_test_path(self, node_name: str, language: str = "python") -> Path:
        if not self._supports_backend_node_tests(language):
            raise ValueError(
                f"single-node test generation is only supported for Python backend nodes, got: {language}"
            )
        return (self._resolve_backend_node_base_dir() / "tests" / f"test_{node_name}.py").resolve()

    def _ensure_node_coder(self, node_name: str) -> Optional[PromptNodeFileCoderBase]:
        if not node_name:
            return None
        coder = self.node_coder_map.get(node_name)
        if coder is not None:
            return coder
        if not self.graph_plan_path:
            return None
        try:
            node_meta = self._load_planned_graph().get_node_meta(node_name)
        except Exception:
            return None
        if node_meta is None:
            return None
        coder = self._make_node_coder(node_meta)
        self._sync_node_coder_root_dir(coder)
        self.node_coder_map[node_name] = coder
        return coder

    def _resolve_amendment_target(
        self,
        file_hint: str,
        *,
        language: str = "python",
    ) -> tuple[Optional[Any], str]:
        normalized_hint = str(file_hint)
        file_name = Path(normalized_hint).name

        if file_name == "main.py" or "main" in file_name:
            return self.main_writer, self.main_output_path or normalized_hint

        for node_name, node_file_location in self.node_location_map.items():
            if (
                normalized_hint == node_file_location
                or node_name in normalized_hint
                or Path(node_file_location).name == file_name
            ):
                coder = self._ensure_node_coder(node_name)
                return coder, node_file_location

        node_name = Path(normalized_hint).stem
        coder = self._ensure_node_coder(node_name)
        if coder is None:
            return None, normalized_hint

        self.node_location_map.setdefault(
            node_name,
            str(self._expected_backend_node_path(node_name, language)),
        )
        return coder, self.node_location_map[node_name]

    def _generate_selected_nodes(
        self,
        node_names: List[str],
        *,
        language: str = "python",
        temperature: float = 0.35,
        reset_mappings: bool = False,
    ) -> List[str]:
        return self._node_build_service.generate_selected_nodes(
            node_names,
            language=language,
            temperature=temperature,
            reset_mappings=reset_mappings,
        )

    def _generate_selected_node_tests(
        self,
        node_names: List[str],
        *,
        language: str = "python",
        temperature: float = 0.2,
    ) -> List[str]:
        return self._node_build_service.generate_selected_node_tests(
            node_names,
            language=language,
            temperature=temperature,
        )

    def _run_selected_node_tests(
        self,
        node_names: List[str],
        *,
        language: str = "python",
        log_filename: str = "node_test_log.txt",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        return self._runtime_service.run_selected_node_tests(
            node_names,
            language=language,
            log_filename=log_filename,
            timeout=timeout,
        )

    def _validate_generated_artifacts(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._main_entrypoint_service.validate_generated_artifacts(
            graph_plan_path=graph_plan_path,
            node_docs_dirname=node_docs_dirname,
            backend_language=backend_language,
            main_entrypoint_path=main_entrypoint_path,
        )

    def _stop_managed_server_process(self, process: Optional[Any]) -> None:
        self._runtime_service.stop_managed_server_process(process)

    def _start_progress(self, total_steps: int) -> None:
        self._progress_total = max(1, total_steps)
        self._progress_current = 0
        self._logger.info(f"Pipeline started. Total steps: {self._progress_total}")
        self._print_progress_bar("Initializing")

    def _advance_progress(self, message: str) -> None:
        self._progress_current = min(self._progress_total, self._progress_current + 1)
        self._print_progress_bar(message)

    def _print_progress_bar(self, message: str) -> None:
        ratio = self._progress_current / self._progress_total if self._progress_total else 0
        filled = int(self._progress_width * ratio)
        bar = "#" * filled + "-" * (self._progress_width - filled)
        pct = int(ratio * 100)
        self._logger.info(f"[{bar}] {self._progress_current}/{self._progress_total} ({pct:3d}%) | {message}")

    def analyze_requirement(self, requirement_text: Optional[str] = None, requirement_file: Optional[str] = None, out_file: str = "requirement_analysis.md") -> str:
        """Produce a requirement analysis markdown file."""
        return self._graph_build_service.analyze_requirement(
            requirement_text=requirement_text,
            requirement_file=requirement_file,
            out_file=out_file,
        )

    def plan_graph(
        self,
        requirement_md_path: Optional[str] = None,
        graph_plan_filename: str = "workflow.json",
        temperature: float = 0.35,
    ) -> str:
        return self._graph_build_service.plan_graph(
            requirement_md_path=requirement_md_path,
            graph_plan_filename=graph_plan_filename,
            temperature=temperature,
        )

    def amend_graph(
        self,
        amendment: str,
        graph_plan_path: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        return self._graph_build_service.amend_graph(
            amendment=amendment,
            graph_plan_path=graph_plan_path,
            temperature=temperature,
        )

    def amend_workflow_json(
        self,
        user_prompt: str,
        workflow_json_path: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        """Amend workflow.json based on a natural-language user prompt.

        This is a convenience wrapper around :meth:`amend_graph` that accepts
        a ``user_prompt`` and targets ``workflow.json`` by default.
        """
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("user_prompt must be a non-empty string.")

        selected_workflow_path = workflow_json_path
        if selected_workflow_path is None:
            selected_workflow_path = self.graph_plan_path
        if selected_workflow_path is None:
            default_workflow_path = self._resolve_root_path("workflow.json")
            if default_workflow_path.is_file():
                selected_workflow_path = str(default_workflow_path)

        if selected_workflow_path is None:
            raise ValueError(
                "workflow_json_path is not set. Call plan_graph(...) first, "
                "set graph_plan_path, or pass workflow_json_path explicitly."
            )

        return self.amend_graph(
            amendment=user_prompt,
            graph_plan_path=selected_workflow_path,
            temperature=temperature,
        )

    def generate_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        language: str = "python",
        temperature: float = 0.35,
    ) -> List[str]:
        return self._node_build_service.generate_nodes(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            language=language,
            temperature=temperature,
        )

    def generate_node_tests(
        self,
        graph_plan_path: Optional[str] = None,
        backend_language: str = "python",
        temperature: float = 0.2,
    ) -> List[str]:
        return self._node_build_service.generate_node_tests(
            graph_plan_path=graph_plan_path,
            backend_language=backend_language,
            temperature=temperature,
        )

    def run_node_tests(
        self,
        graph_plan_path: Optional[str] = None,
        node_names: Optional[List[str]] = None,
        backend_language: str = "python",
        log_filename: str = "node_test_log.txt",
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Run generated per-node pytest files and write summary plus per-node logs."""
        return self._runtime_service.run_node_tests(
            graph_plan_path=graph_plan_path,
            node_names=node_names,
            backend_language=backend_language,
            log_filename=log_filename,
            timeout=timeout,
        )

    def generate_node_markdowns(
        self,
        requirement_md_path: str,
        graph_plan_path: str,
        output_dirname: str = "node_docs",
        temperature: float = 0.2,
    ) -> List[str]:
        return self._node_build_service.generate_node_markdowns(
            requirement_md_path=requirement_md_path,
            graph_plan_path=graph_plan_path,
            output_dirname=output_dirname,
            temperature=temperature,
        )

    def update_nodes_plan(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Dict[str, Any]:
        return self._node_build_service.update_nodes_plan(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            node_docs_dirname=node_docs_dirname,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def update_backend_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        language: str = "python",
        temperature: float = 0.3,
    ) -> Dict[str, Any]:
        return self._node_build_service.update_backend_nodes(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            node_docs_dirname=node_docs_dirname,
            language=language,
            temperature=temperature,
        )

    def get_node_input_output_formats(
        self,
        graph_plan_path: Optional[str] = None,
        backend_language: str = "python",
    ) -> Dict[str, Dict[str, Any]]:
        return self._node_build_service.get_node_input_output_formats(
            graph_plan_path=graph_plan_path,
            backend_language=backend_language,
        )

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
        return self._node_build_service.update_nodes(
            graph_plan_path=graph_plan_path,
            requirement_md_path=requirement_md_path,
            node_docs_dirname=node_docs_dirname,
            language=language,
            temperature=temperature,
            context_base_dir=context_base_dir,
            backend_port=backend_port,
            main_output_filename=main_output_filename,
        )

    def rerun_server(
        self,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
        backend_port: int = 8000,
    ) -> Dict[str, Any]:
        return self._runtime_service.rerun_server(
            graph_plan_path=graph_plan_path,
            node_docs_dirname=node_docs_dirname,
            backend_language=backend_language,
            main_entrypoint_path=main_entrypoint_path,
            backend_port=backend_port,
        )

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
        return self._node_build_service.amend_node_markdown(
            node_name=node_name,
            amendment=amendment,
            existing_markdown_path=existing_markdown_path,
            requirement_md_path=requirement_md_path,
            graph_plan_path=graph_plan_path,
            output_path=output_path,
            temperature=temperature,
            max_tokens=max_tokens,
            overwrite=overwrite,
        )

    def _build_steps_meta(self, include_hidden_nodes: bool = False) -> List[Dict[str, Any]]:
        return build_steps_meta(self, include_hidden_nodes=include_hidden_nodes)

    def _write_main_entrypoint(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        output_filename: str = "main.py",
        fastapi_host: str = "0.0.0.0",
        temperature: float = 0.0,
        fastapi_port: int = 8000,
    ) -> str:
        return self._main_entrypoint_service.write_main_entrypoint(
            graph_plan_path=graph_plan_path,
            output_filename=output_filename,
            fastapi_host=fastapi_host,
            temperature=temperature,
            fastapi_port=fastapi_port,
        )

    def generate_main_entrypoint(
        self,
        graph_plan_path: str,
        output_filename: str = "main.py",
        fastapi_host: str = "0.0.0.0",
        temperature: float = 0.0,
        fastapi_port: int = 8000,
    ) -> str:
        generated_main_output_path = self._write_main_entrypoint(
            graph_plan_path=graph_plan_path,
            output_filename=output_filename,
            fastapi_host=fastapi_host,
            temperature=temperature,
            fastapi_port=fastapi_port,
        )
        self.main_output_path = generated_main_output_path
        return self.main_output_path

    def test_main_entrypoint(
        self,
        main_entrypoint_path: str,
        log_filename: str = "test_log.txt",
        graph_plan_path: Optional[str] = None,
    ) -> bool:
        """Test the generated main_entrypoint.py and write logs to a file.

        This method will exercise the main entrypoint as a subprocess and
        record both stdout and stderr output to a log file.  After the run the
        log is audited and amendments are applied to any code files mentioned
        in the traceback.

        Args:
            main_entrypoint_path: Path to the main_entrypoint.py file to test.
            log_filename: Name of the log file to write output to (under root_dir).
            graph_plan_path: Optional path to the graph plan JSON.  When
            provided the graph is used to decide whether a filename mentioned in
            the log corresponds to a node; this helps select the appropriate
            coder when applying amendments.

        Returns:
            Whether the test was successful. The log file will contain detailed output.
        """
        return self._runtime_service.test_main_entrypoint(
            main_entrypoint_path=main_entrypoint_path,
            log_filename=log_filename,
            graph_plan_path=graph_plan_path,
        )

    def amend_by_log(
        self,
        log_path: str
    ) -> bool:
        return self._runtime_service.amend_by_log(log_path)
