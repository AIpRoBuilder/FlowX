# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified: explicitly release cached native workflow pools on reset/shutdown.
# Modified 2026-10-13: delegate pool cleanup to synchronized service cache owners.
# Modified 2026-10-14: wire explicit service dependencies and refresh components after LLM resets.
# Modified 2026-10-14: graph service delegates to a single-pass workflow without repair-loop injection.
# Modified 2026-10-14: own session bookkeeping and dispatch explicit requests to initialized services.
# Modified 2026-10-14: refresh request graph snapshots and merge repair publications under a builder-owned lock.
# Modified 2026-10-15: use unified workflow requests instead of separate context models.
# Modified 2026-10-15: keep routing local and remove redundant request identities/bookkeeping.
# Modified 2026-10-15: orchestrate batches locally and dispatch complete per-node requests without callback registries.
# Modified 2026-10-15: disable batch node generation and remove batch-only helpers; retain per-node services.
from typing import Optional, List, Dict, Any, Mapping
from contextlib import ExitStack
from dataclasses import replace
import os
import subprocess
from pathlib import Path
from threading import RLock

from flowx_core.architect import GraphPlanner, NodePlanElement, Graph
from flowx_core.auditor import GraphJsonAuditor, NodeAuditor, MainEntryPointAuditor, OutputAuditor
from flowx_core.builder_support import (
    AuditRepairLoop, BuildSession, BuilderComponentFactory,
)
from flowx_core.builder_services import (
    GraphBuildService,
    MainEntrypointService,
    NodeBuildService,
    NodeArtifactService,
    RuntimeService,
    build_steps_meta,
)
from flowx_core.llm_client.coder import compose_session_marking_prompt
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import (
    PromptNodeFileCoderBase,
    RequestNodeFileCoder,
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
from flowx_core.workflows.graph_generation import GraphGenerationRequest
from flowx_core.workflows.main_entrypoint_generation import MainEntrypointGenerationRequest
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    GenerateNodeTestFile,
    NodeRuntimeModifierRequest,
)


DEFAULT_MAX_AUDIT_ROUNDS = 7


class AgentBuilder:
    """Build AG-UI workflow artifacts with generation and auditing."""

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
        self._artifact_publication_lock = RLock()
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

        self._reset_llm_components()
        # initialize auditors for later validation steps
        self.graph_auditor = GraphJsonAuditor()
        self.node_auditor = NodeAuditor()
        self.main_entry_auditor = MainEntryPointAuditor()
        # output auditor will inspect test logs and help trigger amendments
        self.output_auditor = OutputAuditor()
        self._init_services()

    def _init_services(self) -> None:
        """Construct each reusable workflow once and clean up partial initialization."""
        with ExitStack() as cleanup:
            graph_service = GraphBuildService(
                analyzer=self.analyzer, planner=self.planner, graph_auditor=self.graph_auditor,
            )
            cleanup.callback(graph_service.close)
            node_service = NodeBuildService(
                node_planner=self.node_planner, node_coder=self.node_coder, node_auditor=self.node_auditor,
            )
            cleanup.callback(node_service.close)
            artifact_service = NodeArtifactService(
                node_test_writer=self.node_test_writer, node_writer=self.node_modifier_writer,
                run_subprocess=self._run_subprocess, timeout_expired=self._subprocess_timeout_expired(),
            )
            cleanup.callback(artifact_service.close)
            main_service = MainEntrypointService(
                main_writer=self.main_writer, main_entry_auditor=self.main_entry_auditor,
            )
            cleanup.callback(main_service.close)
            self._graph_build_service = graph_service
            self._node_build_service = node_service
            self._node_artifact_service = artifact_service
            self._main_entrypoint_service = main_service
            cleanup.pop_all()
        self._runtime_service = RuntimeService(
            logger=self._logger,
            select_python_command=self._select_python_command,
            popen_subprocess=self._popen_subprocess, run_subprocess=self._run_subprocess,
            timeout_expired=self._subprocess_timeout_expired(),
        )

    def close(self) -> None:
        """Release all four initialized workflows, even if one cleanup fails."""
        with ExitStack() as cleanup:
            for service in (
                self._graph_build_service, self._node_build_service,
                self._node_artifact_service, self._main_entrypoint_service,
            ):
                cleanup.callback(service.close)

    def _request_state(self) -> dict[str, Any]:
        """Seed the leased WorkflowParam.state; services never retain this session."""
        return {"build_session": self._session}

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
            node_planner_cls=NodePlanElement,
            main_writer_cls=PromptMainFileCoder,
            workflow_step_node_coder_cls=WorkflowStepNodeCoder,
            workflow_file_node_coder_cls=WorkflowFileNodeCoder,
            workflow_skill_node_coder_cls=WorkflowSkillNodeCoder,
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
        settings = dict(
            api_key=self.api_key, model=self.model, provider=self.provider,
            session_marking_prompt=self.session_marking_prompt,
        )
        self.node_coder = RequestNodeFileCoder(skills_root_path=self.skills_root_path, **settings)
        self.node_test_writer = GenerateNodeTestFile(**settings)
        self.node_modifier_writer = AmendNodeFromTestLog(**settings)

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
        self.close()
        self._session.node_coder_map.clear()
        self._init_services()

    def _make_node_coder(self, node_meta: Any) -> PromptNodeFileCoderBase:
        return self._component_factory.create_node_coder(node_meta)

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

    def _load_planned_graph(self, graph_plan_path: Optional[str] = None, *, refresh: bool = False) -> Graph:
        """Return a graph snapshot; explicit execution requests refresh same-path files."""
        selected_path = graph_plan_path or self.graph_plan_path
        if graph_plan_path:
            self.graph_plan_path = graph_plan_path
        if not selected_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")

        graph_path = Path(selected_path).expanduser().resolve()
        planned_graph = getattr(self, "planned_graph", None)
        planned_graph_path: Optional[Path] = None
        if planned_graph is not None and hasattr(planned_graph, "graph_json_path"):
            try:
                planned_graph_path = Path(planned_graph.graph_json_path).resolve()
            except Exception:
                planned_graph_path = None
        if refresh or planned_graph is None or planned_graph_path != graph_path:
            self.planned_graph = None
            planned_graph = Graph(str(graph_path))
            self.planned_graph = planned_graph
        # Concurrent named-workflow requests may publish another cache entry;
        # each caller must still receive the snapshot it actually selected.
        return planned_graph

    def _sync_workflow_graph_json(self, context_base_dir: Optional[str] = None) -> str:
        self._load_planned_graph()
        target_dir = Path(self._resolve_managed_path(context_base_dir or self.root_dir))
        source_path = Path(self.graph_plan_path).expanduser().resolve()
        workflow_path = target_dir / "workflow.json"
        workflow_path.parent.mkdir(parents=True, exist_ok=True)
        source_text = source_path.read_text(encoding="utf-8")
        if not workflow_path.exists() or workflow_path.read_text(encoding="utf-8") != source_text:
            workflow_path.write_text(source_text, encoding="utf-8")
        self.workflow_json_path = str(workflow_path)
        return self.workflow_json_path

    def _resolve_managed_path(self, path_value: str) -> str:
        root = Path(self.root_dir).expanduser()
        requested = Path(path_value).expanduser()
        if not requested.is_absolute():
            try:
                requested.relative_to(root)
            except ValueError:
                requested = root / requested
        return str(requested.resolve())

    def _expected_backend_node_path(self, node_name: str, language: str = "python") -> Path:
        return (self._resolve_backend_node_base_dir() / f"{node_name}{get_language_extension(language)}").resolve()

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

    def _validate_generated_artifacts(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        graph = self._load_planned_graph(graph_plan_path)
        doc_dir = self._resolve_root_path(self.node_docs_dir or node_docs_dirname)
        main_path = Path(main_entrypoint_path or self.main_output_path or self._resolve_root_path("main.py")).expanduser()
        if not main_path.is_absolute():
            main_path = self._resolve_root_path(main_path)
        plans, backends, missing = {}, {}, []
        for name in graph.get_topological_sorted_nodes():
            doc_path = doc_dir / f"{name}.md"
            backend_path = self._expected_backend_node_path(name, backend_language)
            if doc_path.is_file():
                plans[name] = str(doc_path)
            else:
                missing.append(str(doc_path))
            if backend_path.is_file():
                backends[name] = str(backend_path)
            else:
                missing.append(str(backend_path))
        if not main_path.is_file():
            missing.append(str(main_path))
        if missing:
            raise FileNotFoundError(
                "Missing artifacts required for graph refresh/server restart:\n" + "\n".join(sorted(set(missing)))
            )
        return {"node_plan": plans, "backend_nodes": backends, "main_entrypoint": str(main_path)}

    def _stop_managed_server_process(self, process: Optional[Any]) -> None:
        self._runtime_service.stop_managed_server_process(process)

    def analyze_requirement(self, requirement_text: Optional[str] = None, requirement_file: Optional[str] = None, out_file: str = "requirement_analysis.md") -> str:
        """Produce a requirement analysis markdown file."""
        out_path = requirement_file or os.path.join(self.root_dir, out_file)
        self._logger.info("%s requirement -> %s", "Using existing" if requirement_file else "Analyzing", out_path)
        self._run_graph_request(GraphGenerationRequest(
            requirement_text=requirement_text or "", requirement_md_path=out_path,
            skip_analyze_requirement=bool(requirement_file), skip_plan_graph=True, skip_audit_graph=True,
        ))
        return self.requirement_md_path

    def _run_graph_request(self, request: GraphGenerationRequest) -> Any:
        """Publish successful graph artifacts in builder state, not in a service."""
        if request.requirement_md_path:
            self.requirement_md_path = request.requirement_md_path
        if not request.skip_plan_graph or (
            request.graph_plan_path and request.graph_plan_path != self.graph_plan_path
        ):
            # A failed same-path audit may follow an on-disk overwrite. Never
            # expose the old topology as if it described the new file.
            self.planned_graph = None
        if request.graph_plan_path:
            self.graph_plan_path = request.graph_plan_path
        result = self._graph_build_service.run(replace(
            request, state={**request.state, **self._request_state()},
        ))
        if not request.skip_analyze_requirement:
            self.requirement_md_path = result.outputs["analyze_requirement"].derived["requirement_md_path"]
        if not request.skip_plan_graph or not request.skip_audit_graph:
            final_step = "plan_graph" if request.skip_audit_graph else "audit_graph"
            self.graph_plan_path = result.outputs[final_step].derived["graph_plan_json_path"]
            self.planned_graph = self._instantiate_graph(self.graph_plan_path)
        return result

    def plan_graph(
        self,
        requirement_md_path: Optional[str] = None,
        graph_plan_filename: str = "workflow.json",
        temperature: float = 0.35,
    ) -> str:
        if requirement_md_path:
            self.requirement_md_path = requirement_md_path
        if not self.requirement_md_path:
            raise ValueError("requirement_md_path is not set. Call analyze_requirement(...) first or pass requirement_md_path.")
        graph_path = os.path.join(self.root_dir, graph_plan_filename)
        self._logger.info("Planning graph -> %s", graph_path)
        self._run_graph_request(GraphGenerationRequest(
            requirement_md_path=self.requirement_md_path, graph_plan_path=graph_path,
            temperature=temperature, skip_analyze_requirement=True,
        ))
        self._logger.info("Graph plan audit passed.")
        return self.graph_plan_path

    def amend_graph(
        self,
        amendment: str,
        graph_plan_path: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        selected_path = graph_plan_path or self.graph_plan_path
        if not selected_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")
        if not isinstance(amendment, str) or not amendment.strip():
            raise ValueError("amendment must be a non-empty string.")
        self._run_graph_request(GraphGenerationRequest(
            graph_plan_path=selected_path, graph_action="amend", amendment=amendment,
            temperature=temperature, skip_analyze_requirement=True,
        ))
        self._logger.info("Graph amendment audit passed.")
        return self.graph_plan_path

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

    def get_node_doc_path(self, node_name: str, *, workflow_name: str = "default") -> str | None:
        """Read a published artifact path without exposing a service's run context."""
        record = self.artifact_state.workflow_artifacts.get(workflow_name)
        return record.node_doc_paths.get(node_name) if record is not None else None

    def generate_nodes(
        self,
        graph_plan_path: Optional[str] = None,
        requirement_md_path: Optional[str] = None,
        language: str = "python",
        temperature: float = 0.35,
        node_names: Optional[List[str]] = None,
        run_nodes: Optional[List[str] | Mapping[str, bool]] = None,
        generate_markdowns: bool = False,
        markdown_amendments: Optional[Mapping[str, str]] = None,
        node_amendments: Optional[Mapping[str, str]] = None,
        workflow_name: str = "default",
    ) -> List[str]:
        """Batch generation is disabled; the per-node workflow and service remain usable."""
        raise NotImplementedError(
            "Batch node generation is not implemented; submit a NodeGenerationRequest "
            "to NodeBuildService.run() instead."
        )

    def run_node_modifier(
        self,
        *,
        workflow_name: str = "default",
        node_name: str,
        graph_plan_path: str | None = None,
        generate_test: bool = True,
        run_test: bool = True,
        amend_node: bool = True,
        timeout: int = 60,
        temperature: float = 0.2,
        amendment_temperature: float = 0.3,
    ) -> NodeRuntimeModifierRequest:
        """Resolve builder artifacts and send a complete repair request to the service."""
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise ValueError("workflow_name must be a non-empty string")
        if not isinstance(node_name, str) or not node_name.strip():
            raise ValueError("node_name must be a non-empty string")
        workflow_name, node_name = workflow_name.strip(), node_name.strip()
        record = self.artifact_state.workflow_artifacts.get(workflow_name)
        selected_path = graph_plan_path or (record.graph_plan_path if record else None)
        if selected_path is None and workflow_name != "default":
            raise ValueError(f"graph_plan_path is required for workflow {workflow_name!r} before it has been generated")
        graph = self._load_planned_graph(selected_path, refresh=True)
        if node_name not in graph.get_topological_sorted_nodes():
            raise ValueError(f"node {node_name!r} not found in workflow {workflow_name!r}")
        workflow_path = Path(graph.graph_json_path).expanduser().resolve()
        same_record = record is not None and Path(record.graph_plan_path).resolve() == workflow_path
        node_path_value = record.node_location_map.get(node_name) if same_record else None
        if not node_path_value:
            mapped = self.node_location_map.get(node_name)
            if mapped and self._resolve_root_path(mapped).parent == workflow_path.parent:
                node_path_value = mapped
        node_path = self._resolve_root_path(node_path_value) if node_path_value else workflow_path.parent / f"{node_name}.py"
        if not node_path.is_file():
            raise FileNotFoundError(f"backend node file not found for workflow {workflow_name!r}, node {node_name!r}: {node_path}")
        with self._artifact_publication_lock:
            workflow_tests = self.dynamic_graph_cache.get("workflow_node_tests", {})
            test_paths = workflow_tests.get(workflow_name, {}) if isinstance(workflow_tests, Mapping) else {}
            if workflow_name == "default" and not test_paths:
                test_paths = self.dynamic_graph_cache.get("node_tests", {})
            cached_test = test_paths.get(node_name) if isinstance(test_paths, Mapping) else None
        component = "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in workflow_name).strip("._") or "workflow"
        default_test = str(Path("tests") / component / f"test_{node_name}.py") if workflow_name != "default" else ""
        request = self._node_artifact_service.run(NodeRuntimeModifierRequest(
            node_file_name=str(node_path), workflow_graph=str(workflow_path),
            working_directory=str(workflow_path.parent), node_name=node_name,
            python_command=self._select_python_command() if run_test else "", timeout=timeout,
            test_temperature=temperature, amendment_temperature=amendment_temperature,
            test_file_name=str(cached_test) if not generate_test and cached_test else default_test,
            log_file_name=str(Path("logs") / component / f"{node_name}_test.log") if workflow_name != "default" else "",
            skip_generate_node_test=not generate_test, skip_run_node_test=not run_test,
            skip_amend_node=not amend_node, state=self._request_state(),
        ))
        # Do not serialize native execution. Merge only completed publications,
        # reading current metadata rather than the pre-execution snapshot.
        with self._artifact_publication_lock:
            if request.test_file_path:
                workflow_tests = self.dynamic_graph_cache.get("workflow_node_tests", {})
                workflow_maps = dict(workflow_tests) if isinstance(workflow_tests, Mapping) else {}
                current_paths = workflow_maps.get(workflow_name, {})
                test_map = dict(current_paths) if isinstance(current_paths, Mapping) else {}
                if workflow_name == "default":
                    legacy_paths = self.dynamic_graph_cache.get("node_tests", {})
                    if isinstance(legacy_paths, Mapping):
                        test_map = {**legacy_paths, **test_map}
                test_map[node_name] = request.test_file_path
                workflow_maps[workflow_name] = test_map
                self.dynamic_graph_cache["workflow_node_tests"] = workflow_maps
                if workflow_name == "default":
                    self.dynamic_graph_cache["node_tests"] = dict(test_map)
            if request.amended_node_file_path:
                self.node_location_map[node_name] = request.amended_node_file_path
                if same_record:
                    record.node_location_map[node_name] = request.amended_node_file_path
        return request

    def rerun_server(
        self,
        graph_plan_path: Optional[str] = None,
        node_docs_dirname: str = "node_docs",
        backend_language: str = "python",
        main_entrypoint_path: Optional[str] = None,
        backend_port: int = 8000,
    ) -> Dict[str, Any]:
        del backend_port
        artifacts = self._validate_generated_artifacts(
            graph_plan_path=graph_plan_path,
            node_docs_dirname=node_docs_dirname,
            backend_language=backend_language,
            main_entrypoint_path=main_entrypoint_path,
        )
        process, backend = self._runtime_service.rerun_server(
            main_entrypoint_path=artifacts["main_entrypoint"], previous_process=self.backend_server_process,
        )
        self.backend_server_process = process
        runtime = {"backend": backend, "artifacts": artifacts}
        self.dynamic_graph_cache["server_runtime"] = runtime
        return runtime

    def _build_steps_meta(self, include_hidden_nodes: bool = False) -> List[Dict[str, Any]]:
        return build_steps_meta(
            self._load_planned_graph(), node_location_map=self.node_location_map,
            resolve_root_path=self._resolve_root_path,
            expected_backend_node_path=self._expected_backend_node_path,
            include_hidden_nodes=include_hidden_nodes,
        )

    def _write_main_entrypoint(
        self,
        *,
        graph_plan_path: Optional[str] = None,
        output_filename: str = "main.py",
        fastapi_host: str = "0.0.0.0",
        fastapi_port: int = 8000,
    ) -> str:
        selected_path = graph_plan_path or self.graph_plan_path
        if not selected_path:
            raise ValueError("graph_plan_path is not set. Call plan_graph(...) first or pass graph_plan_path.")
        self.main_output_path = self._resolve_managed_path(output_filename)
        self._logger.info("Generating main entrypoint -> %s", self.main_output_path)
        self.main_output_path = self._main_entrypoint_service.run(MainEntrypointGenerationRequest(
            project_root_path=self.root_dir, graph_plan_json_path=selected_path,
            output_path=self.main_output_path, fastapi_host=fastapi_host,
            fastapi_port=fastapi_port, state=self._request_state(),
        ))
        self._logger.info("Main entrypoint audit passed.")
        return self.main_output_path

    def generate_main_entrypoint(
        self,
        graph_plan_path: str,
        output_filename: str = "main.py",
        fastapi_host: str = "0.0.0.0",
        temperature: float = 0.0,
        fastapi_port: int = 8000,
    ) -> str:
        # Accept the old public keyword without forwarding it into deterministic
        # template rendering or a native run request.
        del temperature
        generated_main_output_path = self._write_main_entrypoint(
            graph_plan_path=graph_plan_path,
            output_filename=output_filename,
            fastapi_host=fastapi_host,
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
        if graph_plan_path:
            self._load_planned_graph(graph_plan_path)
        self.log_path = os.path.join(self.root_dir, log_filename)
        self._runtime_service.test_main_entrypoint(
            main_entrypoint_path=main_entrypoint_path,
            log_path=self.log_path,
        )
        return self.amend_by_log(self.log_path)

    def amend_by_log(
        self,
        log_path: str
    ) -> bool:
        ok, violations = self.output_auditor.audit_log_file(log_path)
        if not ok:
            for violation in violations:
                coder, target = self._resolve_amendment_target(violation.rule, language="python")
                if coder is None:
                    self._logger.warning("Skipping amendment for unresolved target: %s", violation.rule)
                    continue
                try:
                    self._logger.warning("Applying amendment to %s: %s", violation.rule, violation.detail)
                    coder.amend_code_with_feedback(
                        target, violation.detail, graph_plan_path=self.graph_plan_path or "",
                        requirement_md_path=self.requirement_md_path or "", current_node_name=Path(target).stem,
                        language="python", temperature=0.3,
                    )
                except Exception as exc:
                    self._logger.error("Failed to amend %s: %s", violation.rule, exc, exc_info=True)
        return ok
