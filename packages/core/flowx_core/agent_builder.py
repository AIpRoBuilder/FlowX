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
# Modified 2026-10-15: disable batch node generation and remove batch-only helpers; retain per-node workflows.
# Modified 2026-10-16: publish the node generation workflow and runtime modifier for DynamicWorkflowOrchestrator.
# Modified 2026-10-16: own the request-driven AgentWorkflowEngine and publish the workflows it reads.
# Modified 2026-10-16: remove the unused audit-round settings and node-test coder factory.
# Modified 2026-10-16: own the four generation workflows directly instead of through service wrappers.
# Modified 2026-10-16: keep only workflow.json/requirement, node, main entrypoint, engine and service builders.
from typing import Optional, List, Dict, Any, Mapping
from contextlib import ExitStack
from dataclasses import replace
import os
import subprocess
from pathlib import Path
from threading import RLock

from flowx_core.architect import GraphPlanner, NodePlanElement, Graph
from flowx_core import agent_workflow_engine, dynamic_workflow_orchestrator
from flowx_core.agent_intent import INTENT_SYSTEM_PROMPT
from flowx_core.agent_workflow_engine import AgentResult, AgentWorkflowEngine
from flowx_core.auditor import GraphJsonAuditor, NodeAuditor, MainEntryPointAuditor
from flowx_core.builder_support import BuildSession, BuilderComponentFactory
from flowx_core.builder_services import RuntimeService, build_steps_meta
from flowx_core.llm_client.coder import Coder, compose_session_marking_prompt
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_writer import RequestNodeFileCoder
from flowx_core.demand_analyzer import RequirementDisector
from flowx_core.logging_utils import configure_runtime_logging, get_logger
from flowx_core.tools.agent_builder_tools import (
    get_language_extension,
    select_python_command,
)
from flowx_core.workflows.graph_generation import GraphGenerationRequest, GraphGenerationWorkflow
from flowx_core.workflows.main_entrypoint_generation import (
    MainEntrypointGenerationRequest,
    MainEntrypointGenerationWorkflow,
)
from flowx_core.workflows.node_generation import NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    GenerateNodeTestFile,
    NodeRuntimeModifierPipeline,
)


class AgentBuilder:
    """Build AG-UI workflow artifacts with generation and auditing."""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-flash",
        provider: str = "deepseek",
        root_dir: str = "./example",
        log_level: str | int | None = None,
        log_filename: str = "flowx_core_debug.log",
        session_marking_prompt: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.root_dir = root_dir
        self.session_marking_prompt = compose_session_marking_prompt(session_marking_prompt)
        self._session = BuildSession()
        self._artifact_publication_lock = RLock()
        self._agent: Optional[AgentWorkflowEngine] = None
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
            "Initialized AgentBuilder with model=%s provider=%s root_dir=%s",
            self.model,
            self.provider,
            Path(self.root_dir).expanduser().resolve(),
        )

        self._reset_llm_components()
        # initialize auditors for later validation steps
        self.graph_auditor = GraphJsonAuditor()
        self.node_auditor = NodeAuditor()
        self.main_entry_auditor = MainEntryPointAuditor()
        self._init_services()

    def _init_services(self) -> None:
        """Construct each reusable workflow once and clean up partial initialization."""
        with ExitStack() as cleanup:
            graph = GraphGenerationWorkflow(
                analyzer=self.analyzer, planner=self.planner, graph_auditor=self.graph_auditor,
            )
            cleanup.callback(graph.close)
            node = NodeGenerationWorkflow(
                node_planner=self.node_planner, node_coder=self.node_coder, node_auditor=self.node_auditor,
            )
            cleanup.callback(node.close)
            modifier = NodeRuntimeModifierPipeline(
                node_test_writer=self.node_test_writer, node_writer=self.node_modifier_writer,
                run_subprocess=self._run_subprocess, timeout_expired=self._subprocess_timeout_expired(),
            )
            cleanup.callback(modifier.close)
            main = MainEntrypointGenerationWorkflow(
                main_writer=self.main_writer, main_entry_auditor=self.main_entry_auditor,
            )
            cleanup.callback(main.close)
            self._graph_workflow = graph
            self._node_workflow = node
            self._modifier_pipeline = modifier
            self._main_workflow = main
            cleanup.pop_all()
        # Native generation steps are class-only, so they read these module globals.
        dynamic_workflow_orchestrator.NODE_GENERATION_WORKFLOW = self._node_workflow
        dynamic_workflow_orchestrator.NODE_RUNTIME_MODIFIER_PIPELINE = self._modifier_pipeline
        agent_workflow_engine.GRAPH_GENERATION_WORKFLOW = self._graph_workflow
        agent_workflow_engine.MAIN_ENTRYPOINT_GENERATION_WORKFLOW = self._main_workflow
        self._runtime_service = RuntimeService(
            logger=self._logger,
            select_python_command=self._select_python_command,
            popen_subprocess=self._popen_subprocess, run_subprocess=self._run_subprocess,
            timeout_expired=self._subprocess_timeout_expired(),
        )

    def close(self) -> None:
        """Release all four initialized workflows, even if one cleanup fails."""
        # Unpublish first, and only our own, so no dynamic run starts on a closing workflow.
        if dynamic_workflow_orchestrator.NODE_GENERATION_WORKFLOW is self._node_workflow:
            dynamic_workflow_orchestrator.NODE_GENERATION_WORKFLOW = None
        if dynamic_workflow_orchestrator.NODE_RUNTIME_MODIFIER_PIPELINE is self._modifier_pipeline:
            dynamic_workflow_orchestrator.NODE_RUNTIME_MODIFIER_PIPELINE = None
        if agent_workflow_engine.GRAPH_GENERATION_WORKFLOW is self._graph_workflow:
            agent_workflow_engine.GRAPH_GENERATION_WORKFLOW = None
        if agent_workflow_engine.MAIN_ENTRYPOINT_GENERATION_WORKFLOW is self._main_workflow:
            agent_workflow_engine.MAIN_ENTRYPOINT_GENERATION_WORKFLOW = None
        with ExitStack() as cleanup:
            for workflow in (
                self._graph_workflow, self._node_workflow, self._modifier_pipeline, self._main_workflow,
            ):
                cleanup.callback(workflow.close)
            # Created on first use, so absent when the engine was never requested; closed first.
            agent, self._agent = getattr(self, "_agent", None), None
            if agent is not None:
                cleanup.callback(agent.close)

    @property
    def engine(self) -> AgentWorkflowEngine:
        """Request-driven engine (reformat -> graph -> nodes -> main entrypoint), created on first use.

        It reads the workflows published by this builder, so it is closed with the builder
        (and by ``reset_llm_config``; its workflows reload from disk on the next use).
        """
        with self._artifact_publication_lock:
            if self._agent is None:
                self._agent = AgentWorkflowEngine(
                    storage_dir=Path(self.root_dir).expanduser().resolve() / "workflows",
                    judge=self._judge_intent, runtime=lambda: self._runtime_service,
                )
            return self._agent

    def _judge_intent(self, prompt: str) -> str:
        return Coder(
            api_key=self.api_key, model=self.model, provider=self.provider,
            system_prompt=INTENT_SYSTEM_PROMPT,
        ).generate_code(prompt, temperature=0.0, max_tokens=2000)

    def build_workflow_json(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Create or modify workflow.json and its requirements; see ``AgentWorkflowEngine``."""
        return self.engine.build_workflow_json(*args, **kwargs)

    def build_nodes(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Create or modify a workflow's nodes, or specific nodes by step id."""
        return self.engine.build_nodes(*args, **kwargs)

    def build_main_entrypoint(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Create or modify a workflow's main entrypoint."""
        return self.engine.build_main_entrypoint(*args, **kwargs)

    def build_workflow(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Create or modify a whole workflow from a request and/or output logs."""
        return self.engine.build_workflow(*args, **kwargs)

    def run_service(self, workflow_id: str) -> Dict[str, Any]:
        """Run or rerun a built workflow as a service through the runtime service."""
        return self.engine.run_service(workflow_id)

    def stop_service(self, workflow_id: str) -> None:
        self.engine.stop_service(workflow_id)

    def _request_state(self) -> dict[str, Any]:
        """Seed the leased WorkflowParam.state; workflows never retain this session."""
        return {"build_session": self._session}

    @property
    def artifact_state(self) -> Any:
        return self._session.artifacts

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
    def node_location_map(self) -> Dict[str, str]:
        return self._session.node_location_map

    @node_location_map.setter
    def node_location_map(self, value: Dict[str, str]) -> None:
        self._session.node_location_map = value

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
            session_marking_prompt=self.session_marking_prompt,
            analyzer_cls=RequirementDisector,
            planner_cls=GraphPlanner,
            node_planner_cls=NodePlanElement,
            main_writer_cls=PromptMainFileCoder,
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
        self.node_coder = RequestNodeFileCoder(**settings)
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
        self._init_services()

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
        """Publish successful graph artifacts in builder state, not in the workflow."""
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
        result = self._graph_workflow.run(replace(
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

    def amend_workflow_json(
        self,
        user_prompt: str,
        workflow_json_path: Optional[str] = None,
        temperature: float = 0.35,
    ) -> str:
        """Amend workflow.json based on a natural-language user prompt.

        Targets the planned graph, or ``workflow.json`` under the root, by default.
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

        self._run_graph_request(GraphGenerationRequest(
            graph_plan_path=selected_workflow_path, graph_action="amend", amendment=user_prompt,
            temperature=temperature, skip_analyze_requirement=True,
        ))
        self._logger.info("Graph amendment audit passed.")
        return self.graph_plan_path

    def get_node_doc_path(self, node_name: str, *, workflow_name: str = "default") -> str | None:
        """Read a published artifact path from builder state."""
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
        """Batch generation is disabled; per-node generation remains available through build_nodes()."""
        raise NotImplementedError(
            "Batch node generation is not implemented; use build_nodes() to generate a workflow's nodes."
        )

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
        self.main_output_path = self._main_workflow.run(MainEntrypointGenerationRequest(
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
