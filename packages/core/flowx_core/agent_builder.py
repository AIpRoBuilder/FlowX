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
# Modified 2026-10-28: remove the legacy single-pass builder API now owned by AgentWorkflowEngine.
from typing import Optional, Dict, Any
from contextlib import ExitStack
import os
import subprocess
from pathlib import Path
from threading import RLock

from flowx_core.architect import GraphPlanner, NodePlanElement
from flowx_core import agent_workflow_engine, dynamic_workflow_orchestrator
from flowx_core.agent_intent import INTENT_SYSTEM_PROMPT
from flowx_core.agent_workflow_engine import AgentResult, AgentWorkflowEngine
from flowx_core.auditor import GraphJsonAuditor, NodeAuditor, MainEntryPointAuditor
from flowx_core.builder_support import BuilderComponentFactory
from flowx_core.builder_services import RuntimeService
from flowx_core.llm_client.coder import Coder, compose_session_marking_prompt
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_writer import RequestNodeFileCoder
from flowx_core.demand_analyzer import RequirementDisector
from flowx_core.logging_utils import configure_runtime_logging, get_logger
from flowx_core.tools.agent_builder_tools import select_python_command
from flowx_core.workflows.graph_generation import GraphGenerationWorkflow
from flowx_core.workflows.main_entrypoint_generation import MainEntrypointGenerationWorkflow
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

    def evolve(self, *args: Any, **kwargs: Any) -> AgentResult:
        """Evolve a workflow, or one node of it, by a command; without a target the engine judges."""
        return self.engine.evolve(*args, **kwargs)

    def run_service(self, workflow_id: str) -> Dict[str, Any]:
        """Run or rerun a built workflow as a service through the runtime service."""
        return self.engine.run_service(workflow_id)

    def stop_service(self, workflow_id: str) -> None:
        self.engine.stop_service(workflow_id)

    def _select_python_command(self) -> str:
        return select_python_command()

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

