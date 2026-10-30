# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Modified 2026-10-22: record the service bind address and expose its connectable base URL.
"""Request-driven workflow engine: reformat request -> graph plan -> nodes -> main entrypoint.

The engine's own native graph runs ``reformat_request -> graph_plan``. The nodes stage and the
main entrypoint are not steps here: ``DynamicWorkflowOrchestrator`` (one per workflow id) is an
``ExpandedWorkflowEngine`` whose graph contains every node build chain, a transit
``nodes_stage_exit`` step that publishes the revision, and a continuation holding
``main_entrypoint_generation``. The engine seeds the run payload and the previous stage's context
into that run, then reads the merged stage records back.

Like the orchestrator it is a native WorkflowEngine whose class-only steps read module globals
published by ``AgentBuilder``: the graph and main-entrypoint workflows. Each stage retries until
it reports no error or its retry budget is spent; failures are returned in the result and
retained per workflow so a later request can repair them. The engine keeps the map of workflow
ids, node ids, main entrypoint ids and their artifacts; successive requests find the workflow to
modify through it.
"""

from __future__ import annotations

import json
import shutil
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Lock, RLock
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, TypeVar

from ag_ui_workflow import GraphSpec, StepRunOutput, WorkflowStepNode, rebind_step_class

from flowx_core.agent_intent import (
	AgentIntent,
	StagePlan,
	build_intent,
	error_key,
	fix_text,
	join_text,
	match_log_to_stage,
	resolve_intent,
)
from flowx_core.auditor import OutputAuditor
from flowx_core.dynamic_workflow_orchestrator import (
	DynamicWorkflowError,
	DynamicWorkflowOrchestrator,
	DynamicWorkflowRequest,
	WorkflowVersion,
)
from flowx_core.tools.artifact_tools import text_digest, write_json_atomic
from flowx_core.tools.text_tools import truncate_context
from flowx_core.tools.workflow_engine import (
	FlowXWorkflowEngine,
	close_workflow_engine,
	init_workflow_engine,
)
from flowx_core.workflows.graph_generation import GraphGenerationRequest, GraphGenerationWorkflow
from flowx_core.workflows.main_entrypoint_generation import (
	MainEntrypointGenerationRequest,
	MainEntrypointGenerationWorkflow,
)

if TYPE_CHECKING:
	from flowx_core.builder_services import RuntimeService

# Published by AgentBuilder: class-only native steps cannot take constructor arguments.
GRAPH_GENERATION_WORKFLOW: GraphGenerationWorkflow | None = None
MAIN_ENTRYPOINT_GENERATION_WORKFLOW: MainEntrypointGenerationWorkflow | None = None
# One orchestrator per workflow id; each engine registers the workflows it owns.
ORCHESTRATORS: dict[str, DynamicWorkflowOrchestrator] = {}
_ORCHESTRATORS_LOCK = Lock()

STAGE_NAMES = ("reformat", "graph", "nodes", "main")
DEFAULT_RETRIES = 1
T = TypeVar("T")


def _published() -> tuple[GraphGenerationWorkflow, MainEntrypointGenerationWorkflow]:
	if GRAPH_GENERATION_WORKFLOW is None or MAIN_ENTRYPOINT_GENERATION_WORKFLOW is None:
		raise RuntimeError("AgentBuilder has not published its graph and main entrypoint workflows")
	return GRAPH_GENERATION_WORKFLOW, MAIN_ENTRYPOINT_GENERATION_WORKFLOW


def _orchestrator(workflow_id: str) -> DynamicWorkflowOrchestrator:
	with _ORCHESTRATORS_LOCK:
		if workflow_id not in ORCHESTRATORS:
			raise KeyError(f"no orchestrator for workflow {workflow_id}")
		return ORCHESTRATORS[workflow_id]


@dataclass(frozen=True, kw_only=True)
class AgentRequest:
	"""Free text and/or output logs, or an explicit intent that skips the LLM judge.

	``max_retries`` counts extra attempts after a failure, for every stage or per stage
	(``reformat``, ``graph``, ``nodes``, ``main``). With ``cascade`` a change in one stage
	also runs the later stages that depend on it. ``fix_errors`` adds the errors retained
	from earlier runs of the workflow; ``node_options`` are extra ``DynamicWorkflowRequest``
	fields (temperatures, ``force_nodes``, ``node_formats`` ...).

	``target`` names what ``prompt`` evolves: ``"workflow"`` for the workflow itself, or a node
	step id or node id of ``workflow_id`` for that node only. Without it the judge decides.
	"""

	prompt: str = ""
	workflow_id: str | None = None
	target: str | None = None
	logs: tuple[str, ...] = ()
	fix_errors: bool = False
	intent: AgentIntent | None = None
	cascade: bool = True
	max_retries: int | Mapping[str, int] = DEFAULT_RETRIES
	temperature: float = 0.35
	fastapi_host: str = "0.0.0.0"
	fastapi_port: int = 8000
	node_options: Mapping[str, Any] = field(default_factory=dict)
	smoke_test_main: bool = False
	serve: bool = False

	def __post_init__(self) -> None:
		if self.target is not None and (not isinstance(self.target, str) or not self.target.strip()):
			raise ValueError("target must be a non-empty string")
		if self.target is not None and self.intent is not None:
			raise ValueError("pass either target or intent, not both")
		object.__setattr__(self, "logs", (self.logs,) if isinstance(self.logs, str) else tuple(self.logs))
		limits = [self.max_retries] if isinstance(self.max_retries, int) else list(self.max_retries.values())
		if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in limits):
			raise ValueError("max_retries must be non-negative integers")
		if not isinstance(self.max_retries, int) and set(self.max_retries).difference(STAGE_NAMES):
			raise ValueError(f"max_retries stages must be among {STAGE_NAMES}")

	def retries(self, stage: str) -> int:
		if isinstance(self.max_retries, int):
			return self.max_retries
		return self.max_retries.get(stage, DEFAULT_RETRIES)


@dataclass(frozen=True)
class StageOutcome:
	stage: str
	status: Literal["completed", "skipped", "failed", "blocked"]
	attempts: int = 0
	error: str = ""
	changed: bool = False
	artifacts: Mapping[str, Any] = field(default_factory=dict)

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


@dataclass(frozen=True)
class AgentResult:
	"""Everything the next request needs: outcomes, unresolved errors, run logs and artifacts."""

	workflow_id: str
	ok: bool
	intent: AgentIntent | None
	stages: Mapping[str, StageOutcome]
	errors: Mapping[str, str]
	logs: Mapping[str, Any]
	artifacts: Mapping[str, Any]
	service: Mapping[str, Any] | None = None

	def raise_for_failure(self) -> AgentResult:
		if not self.ok:
			raise RuntimeError("agent request failed:\n" + "\n".join(
				f"{name}: {text}" for name, text in self.errors.items()
			))
		return self


@dataclass
class WorkflowRecord:
	"""One workflow the agent can find again: its ids, artifact locations and unresolved errors.

	Errors are keyed ``graph``, ``main`` or ``nodes/<step id>`` and cleared when that part
	next succeeds.
	"""

	workflow_id: str
	storage_dir: Path
	main_entrypoint_id: str
	main_entrypoint_version: int = 0
	main_workflow_version: int = 0
	main_entrypoint_path: Path | None = None
	fastapi_host: str = "0.0.0.0"
	fastapi_port: int = 8000
	errors: dict[str, str] = field(default_factory=dict)
	process: Any = field(default=None, repr=False)

	@property
	def graph_path(self) -> Path:
		return self.storage_dir / "plan" / "workflow.json"

	@property
	def requirement_path(self) -> Path:
		return self.storage_dir / "plan" / "requirement_analysis.md"

	@property
	def service_dir(self) -> Path:
		# Importable package name: the generated main imports its nodes from it.
		return self.storage_dir / "service" / f"service_{self.main_entrypoint_id}"

	@property
	def main_log_path(self) -> Path:
		return self.storage_dir / "logs" / "main_run.log"

	@property
	def base_url(self) -> str:
		"""Where a client reaches the running service; bind addresses map to loopback."""
		host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(self.fastapi_host, self.fastapi_host)
		if ":" in host and not host.startswith("["):
			host = f"[{host}]"
		return f"http://{host}:{self.fastapi_port}"

	def clear_errors(self, prefix: str) -> None:
		for key in [key for key in self.errors if key == prefix or key.startswith(f"{prefix}/")]:
			del self.errors[key]

	def save(self) -> None:
		write_json_atomic(self.storage_dir / "agent.json", {
			"workflow_id": self.workflow_id,
			"main_entrypoint_id": self.main_entrypoint_id,
			"main_entrypoint_version": self.main_entrypoint_version,
			"main_workflow_version": self.main_workflow_version,
			"main_entrypoint_path": str(self.main_entrypoint_path) if self.main_entrypoint_path else None,
			"fastapi_host": self.fastapi_host,
			"fastapi_port": self.fastapi_port,
			"errors": self.errors,
		})

	@classmethod
	def load(cls, storage_dir: Path) -> WorkflowRecord:
		data = json.loads((storage_dir / "agent.json").read_text(encoding="utf-8"))
		return cls(
			workflow_id=data["workflow_id"], storage_dir=storage_dir,
			main_entrypoint_id=data["main_entrypoint_id"],
			main_entrypoint_version=data["main_entrypoint_version"],
			main_workflow_version=data["main_workflow_version"],
			main_entrypoint_path=Path(data["main_entrypoint_path"]) if data["main_entrypoint_path"] else None,
			fastapi_host=data.get("fastapi_host", "0.0.0.0"),
			fastapi_port=int(data.get("fastapi_port", 8000)),
			errors=dict(data["errors"]),
		)


def _error_text(exc: BaseException) -> str:
	return f"{type(exc).__name__}: {exc}"


def _with_retries(retries: int, action: Callable[[int, str], T]) -> tuple[T | None, int, str]:
	"""Call ``action(attempt, previous_error)`` until it returns; each failure earns one more try."""
	error = ""
	for attempt in range(1, retries + 2):
		try:
			return action(attempt, error), attempt, ""
		except Exception as exc:
			error = _error_text(exc)
	return None, retries + 1, error


def _enabled_nodes(graph_path: Path) -> list[str]:
	try:
		nodes = json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]
	except (OSError, ValueError, KeyError, TypeError):
		return []
	return [
		node["name"] for node in nodes
		if isinstance(node, dict) and node.get("enable", True) and isinstance(node.get("name"), str)
	]


def _digest(*paths: Path) -> str:
	return text_digest("\0".join(path.read_text(encoding="utf-8") if path.is_file() else "" for path in paths))


@dataclass(frozen=True)
class _AgentRun:
	"""Request-local payload for the native steps; pooled step instances retain nothing."""

	request: AgentRequest
	agent: AgentWorkflowEngine


def _agent_run(session_state: Mapping[str, Any]) -> _AgentRun:
	run = session_state.get("agent_run")
	if not isinstance(run, _AgentRun):
		raise TypeError("agent run is missing")
	return run


class _ReformatStep(WorkflowStepNode):
	"""Decide what to create or modify (rules for logs, the judge for free text) and find the workflow."""

	STEP_ID = "reformat_request"
	TITLE = "Reformat request"
	DEPENDENCIES: list[str] = []
	INPUT_REQUIRED = False

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		run = _agent_run(session_state)
		request, agent = run.request, run.agent

		def attempt(_attempt: int, failure: str) -> AgentIntent:
			catalog = agent.catalog()
			if request.intent is not None:
				return resolve_intent(request.intent, catalog)
			return build_intent(
				prompt=request.prompt, logs=request.logs, fix_errors=request.fix_errors,
				workflow_id=request.workflow_id, target=request.target, catalog=catalog,
				judge=agent.judge, feedback=failure,
			)

		intent, attempts, error = _with_retries(request.retries("reformat"), attempt)
		if intent is not None:
			try:
				record = agent.resolve_record(intent.workflow_id)
			except Exception as exc:
				intent, error = None, _error_text(exc)
		if intent is None:
			outcome = StageOutcome("reformat", "failed", attempts, error)
			derived = {"workflow_id": "", "intent": None}
		else:
			intent = replace(intent, workflow_id=record.workflow_id)
			outcome = StageOutcome("reformat", "completed", attempts, artifacts={"workflow_id": record.workflow_id})
			derived = {"workflow_id": record.workflow_id, "intent": intent.to_dict()}
		return StepRunOutput(
			card={"stage": "reformat", "status": outcome.status},
			derived={**derived, "stages": {"reformat": outcome.to_dict()}, "error": error},
		)


class _StageStep(WorkflowStepNode):
	"""Run one build stage on the context its parent passed on; stage failures are data, not exceptions."""

	INPUT_REQUIRED = False
	STAGE = ""

	def execute(self, run: _AgentRun, record: WorkflowRecord, intent: AgentIntent) -> StageOutcome:
		raise NotImplementedError

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input
		run = _agent_run(session_state)
		context = dict(next(iter(dependency_results.values())).derived)
		if context["error"]:
			outcome = StageOutcome(self.STAGE, "blocked")
		else:
			try:
				record = run.agent.record(context["workflow_id"])
				outcome = self.execute(run, record, AgentIntent.from_dict(context["intent"]))
				record.save()
			except Exception as exc:
				outcome = StageOutcome(self.STAGE, "failed", error=_error_text(exc))
		failure = (outcome.error or "failed") if outcome.status == "failed" else ""
		return StepRunOutput(
			card={"stage": self.STAGE, "status": outcome.status},
			derived={
				**context,
				"stages": {**context["stages"], self.STAGE: outcome.to_dict()},
				"error": context["error"] or failure,
			},
		)


def _graph_request(
	request: AgentRequest, record: WorkflowRecord, intent: AgentIntent, failure: str,
) -> GraphGenerationRequest:
	graph, requirement, plan = record.graph_path, record.requirement_path, intent.graph
	common = {
		"graph_plan_path": str(graph), "requirement_md_path": str(requirement),
		"temperature": request.temperature,
	}
	if failure and graph.is_file():
		# The graph exists but did not pass: repair it from the failure, keep the requirement.
		return GraphGenerationRequest(
			**common, graph_action="amend", amendment=fix_text(failure), skip_analyze_requirement=True,
		)
	if plan.mode == "create":
		return GraphGenerationRequest(**common, requirement_text=intent.requirement)
	if not graph.is_file():
		if not requirement.is_file():
			raise ValueError(f"workflow {record.workflow_id} has no graph or requirement to modify; create it first")
		return GraphGenerationRequest(**common, skip_analyze_requirement=True)
	return GraphGenerationRequest(
		**common, graph_action="amend", amendment=plan.amendment,
		requirement_action="amend", requirement_amendment=plan.instruction,
		skip_analyze_requirement=not (plan.instruction and requirement.is_file()),
	)


class _GraphStep(_StageStep):
	STEP_ID = "graph_plan"
	TITLE = "Plan graph"
	STAGE = "graph"
	DEPENDENCIES = ["reformat_request"]

	def execute(self, run: _AgentRun, record: WorkflowRecord, intent: AgentIntent) -> StageOutcome:
		if intent.graph.mode == "skip":
			return StageOutcome(self.STAGE, "skipped")
		generation, _ = _published()
		before = _digest(record.graph_path, record.requirement_path)
		_, attempts, error = _with_retries(
			run.request.retries(self.STAGE),
			lambda _attempt, failure: generation.run(_graph_request(run.request, record, intent, failure)),
		)
		if error:
			record.errors["graph"] = error
			return StageOutcome(self.STAGE, "failed", attempts, error)
		record.clear_errors("graph")
		return StageOutcome(
			self.STAGE, "completed", attempts,
			changed=_digest(record.graph_path, record.requirement_path) != before,
			artifacts={"graph": str(record.graph_path), "requirement": str(record.requirement_path)},
		)


def _failure_text(node: str, error: str, logs: Any) -> str:
	parts = [error]
	for label, text in (("Test log", logs.test.get(node)), ("Audit log", logs.audit.get(node))) if logs else ():
		if text:
			parts.append(f"{label}:\n{text}")
	return truncate_context("\n".join(parts), label="node failure", max_chars=6000)


def _nodes_stale(record: WorkflowRecord, orchestrator: DynamicWorkflowOrchestrator) -> bool:
	"""The plan graph or requirement differs from the latest published workflow version."""
	if not record.graph_path.is_file():
		return False
	try:
		version = orchestrator.get_workflow_version()
	except KeyError:
		return True
	def snapshot(graph: Path, requirement: Path) -> tuple[Any, str]:
		return (
			json.loads(graph.read_text(encoding="utf-8")),
			requirement.read_text(encoding="utf-8") if requirement.is_file() else "",
		)
	return snapshot(record.graph_path, record.requirement_path) != snapshot(
		version.workflow_path, version.requirement_md_path,
	)


def _materialize(version: WorkflowVersion, package_dir: Path) -> None:
	"""Copy the published bundle into an importable package that pruning cannot remove."""
	package_dir.mkdir(parents=True, exist_ok=True)
	for stale in package_dir.glob("*.py"):
		stale.unlink()
	shutil.copy2(version.workflow_path, package_dir / "workflow.json")
	for name, node in version.node_versions.items():
		shutil.copy2(node.source_path, package_dir / f"{name}.py")


class _MainStep(_StageStep):
	STEP_ID = "main_entrypoint_generation"
	TITLE = "Generate main entrypoint"
	STAGE = "main"
	# Rebound by the orchestrator after its transit nodes-stage exit step.
	DEPENDENCIES: list[str] = []

	def execute(self, run: _AgentRun, record: WorkflowRecord, intent: AgentIntent) -> StageOutcome:
		plan, request, agent = intent.main, run.request, run.agent
		orchestrator = _orchestrator(record.workflow_id)
		published = orchestrator.workflow_version
		exists = record.main_entrypoint_path is not None and record.main_entrypoint_path.is_file()
		stale = published > 0 and (not exists or record.main_workflow_version != published)
		if not (plan.mode != "skip" or (request.cascade and stale)):
			return StageOutcome(self.STAGE, "skipped")
		_, generation = _published()
		try:
			version = orchestrator.get_workflow_version()
		except KeyError:
			raise ValueError(f"workflow {record.workflow_id} has no generated nodes; build them first") from None
		unplaced: list[str] = []

		def attempt(_attempt: int, _failure: str) -> tuple[str, str]:
			package = record.service_dir
			_materialize(version, package)
			path = generation.run(MainEntrypointGenerationRequest(
				project_root_path=str(package), graph_plan_json_path=str(package / "workflow.json"),
				output_path=str(package / "main.py"),
				fastapi_host=request.fastapi_host, fastapi_port=request.fastapi_port,
			))
			if not request.smoke_test_main:
				return path, ""
			ok, text = agent.smoke_test(record, path)
			if not ok:
				unplaced[:] = [text]
				raise RuntimeError(text)
			return path, text

		generated, attempts, error = _with_retries(request.retries(self.STAGE), attempt)
		if generated is None:
			# A failing run log may belong to a node rather than to the entrypoint.
			match = match_log_to_stage(unplaced[0], _enabled_nodes(record.graph_path)) if unplaced else None
			record.errors[error_key(*match) if match else "main"] = error
			return StageOutcome(self.STAGE, "failed", attempts, error)
		path, run_log = generated
		record.main_entrypoint_path, record.main_entrypoint_version = Path(path), record.main_entrypoint_version + 1
		record.main_workflow_version = version.version
		record.fastapi_host, record.fastapi_port = request.fastapi_host, request.fastapi_port
		record.clear_errors("main")
		return StageOutcome(
			self.STAGE, "completed", attempts, changed=True,
			artifacts={
				"main_entrypoint_id": record.main_entrypoint_id,
				"main_entrypoint_version": record.main_entrypoint_version,
				"main_entrypoint_path": path, "workflow_version": version.version, "run_log": run_log,
			},
		)


# The agent engine runs reformat -> graph natively; the orchestrator then runs the
# expanded node chains and, through this continuation graph, the main entrypoint.
_HEAD_STEP_CLASSES = {step.STEP_ID: step for step in (_ReformatStep, _GraphStep)}
_GRAPH_STAGE = _GraphStep.STEP_ID
_MAIN_STEP = _MainStep.STEP_ID
_MAIN_ROOT_CLASS = rebind_step_class(_MainStep, dependencies=[])
_MAIN_CONTINUATION = GraphSpec(
	nodes=[{"name": _MAIN_STEP}],
	classes={_MAIN_STEP: _MAIN_ROOT_CLASS},
	steps_meta=[_MAIN_ROOT_CLASS.step_meta()],
)


class AgentWorkflowEngine:
	"""Build and modify workflows from requests, and keep the map of what was built.

	Runs are serialized. ``judge`` maps a prompt to the LLM's answer and ``runtime`` returns the
	``RuntimeService`` used to run generated services. Closing the engine stops the services it started.
	"""

	def __init__(
		self,
		*,
		storage_dir: str | Path,
		judge: Callable[[str], str] | None = None,
		runtime: Callable[[], RuntimeService] | None = None,
		keep_last_versions: int | None = None,
		pool_size: int = 1,
	) -> None:
		self.storage_dir = Path(storage_dir).expanduser().resolve()
		self.storage_dir.mkdir(parents=True, exist_ok=True)
		self.judge, self._runtime = judge, runtime
		self.log_auditor = OutputAuditor()
		self._keep_last_versions = keep_last_versions
		self._records: dict[str, WorkflowRecord] = {}
		self._registry_lock, self._run_lock = RLock(), RLock()
		self._closed = False
		self._engine: FlowXWorkflowEngine | None = init_workflow_engine(
			_HEAD_STEP_CLASSES,
			{
				"pool_size": pool_size,
				"nodes": [{"name": step.STEP_ID, "depends": list(step.DEPENDENCIES)} for step in _HEAD_STEP_CLASSES.values()],
			},
			graph_config_id="flowx-agent",
		)
		try:
			for marker in sorted(self.storage_dir.glob("*/agent.json")):
				self._register(WorkflowRecord.load(marker.parent))
		except BaseException:
			self.close()
			raise

	def _register(self, record: WorkflowRecord) -> None:
		orchestrator = DynamicWorkflowOrchestrator(
			storage_dir=record.storage_dir, workflow_id=record.workflow_id,
			keep_last_versions=self._keep_last_versions,
			continuation=_MAIN_CONTINUATION,
		)
		with _ORCHESTRATORS_LOCK:
			if record.workflow_id in ORCHESTRATORS:
				orchestrator.close()
				raise ValueError(f"workflow {record.workflow_id} is already registered by another agent")
			ORCHESTRATORS[record.workflow_id] = orchestrator
		with self._registry_lock:
			self._records[record.workflow_id] = record

	@property
	def workflow_ids(self) -> tuple[str, ...]:
		with self._registry_lock:
			return tuple(self._records)

	def record(self, workflow_id: str) -> WorkflowRecord:
		with self._registry_lock:
			if workflow_id not in self._records:
				raise KeyError(f"unknown workflow_id: {workflow_id}")
			return self._records[workflow_id]

	def resolve_record(self, workflow_id: str | None) -> WorkflowRecord:
		"""The record to build into; ``None`` starts a new workflow."""
		if workflow_id is not None:
			return self.record(workflow_id)
		new_id = uuid.uuid4().hex
		record = WorkflowRecord(new_id, self.storage_dir / new_id, main_entrypoint_id=uuid.uuid4().hex)
		self._register(record)
		record.save()
		return record

	def catalog(self) -> dict[str, dict[str, Any]]:
		"""Ids and unresolved errors of every workflow: what the judge matches a request against."""
		with self._registry_lock:
			records = list(self._records.values())
		result = {}
		for record in records:
			orchestrator = _orchestrator(record.workflow_id)
			published = orchestrator.generated_node_id_map
			result[record.workflow_id] = {
				"nodes": {name: published.get(name) for name in _enabled_nodes(record.graph_path)} or published,
				"workflow_version": orchestrator.workflow_version,
				"main_entrypoint_id": record.main_entrypoint_id,
				"has_graph": record.graph_path.is_file(),
				"has_main": bool(record.main_entrypoint_path and record.main_entrypoint_path.is_file()),
				"errors": dict(record.errors),
			}
		return result

	def artifacts(self, workflow_id: str) -> dict[str, Any]:
		"""Locations of a workflow's requirement, graph, node versions and main entrypoint."""
		record = self.record(workflow_id)
		try:
			version = _orchestrator(workflow_id).get_workflow_version()
		except KeyError:
			version = None
		main = record.main_entrypoint_path
		return {
			"workflow_id": workflow_id,
			"workflow_version": version.version if version else 0,
			"requirement": str(record.requirement_path) if record.requirement_path.is_file() else None,
			"graph": str(record.graph_path) if record.graph_path.is_file() else None,
			"nodes": {
				name: {
					"node_id": node.node_id, "version": node.version,
					"artifacts": {key: str(path) for key, path in node.artifacts.items()},
				}
				for name, node in (version.node_versions.items() if version else ())
			},
			"main_entrypoint": {
				"id": record.main_entrypoint_id, "version": record.main_entrypoint_version,
				"path": str(main) if main and main.is_file() else None,
			},
		}

	def list_nodes(self, workflow_id: str) -> list[dict[str, Any]]:
		"""The workflow's enabled nodes as ``{step_id, node_id, version}``; ``node_id`` is None until built."""
		record = self.record(workflow_id)
		try:
			versions = _orchestrator(workflow_id).get_workflow_version().node_versions
		except KeyError:
			versions = {}
		steps = _enabled_nodes(record.graph_path) or list(versions)
		return [
			{
				"step_id": name,
				"node_id": versions[name].node_id if name in versions else None,
				"version": versions[name].version if name in versions else 0,
			}
			for name in steps
		]

	def runtime(self) -> RuntimeService:
		if self._runtime is None:
			raise RuntimeError("no runtime service is configured")
		return self._runtime()

	def smoke_test(self, record: WorkflowRecord, main_path: str) -> tuple[bool, str]:
		"""Run the entrypoint, audit its run log; return ``(ok, log or error report)``."""
		log_path = record.main_log_path
		self.runtime().test_main_entrypoint(main_entrypoint_path=main_path, log_path=str(log_path))
		text = log_path.read_text(encoding="utf-8")
		ok, violations = self.log_auditor.audit_log_file(str(log_path))
		if ok:
			return True, text
		report = "\n".join(f"{item.rule}: {item.detail}" for item in violations)
		return False, f"main entrypoint run log reported errors:\n{report}\n\n" + truncate_context(
			text, label="run log", max_chars=6000,
		)

	def run(self, request: AgentRequest) -> AgentResult:
		"""Reformat the request, then build what it asks for; stage failures are returned, not raised."""
		with self._run_lock:
			if self._closed or self._engine is None:
				raise RuntimeError("agent workflow engine is closed")
			_published()
			head = self._engine.run_steps(state={"agent_run": _AgentRun(request, self)})
			final = dict(head.outputs[_GRAPH_STAGE].derived)
			if not final["error"]:
				final = self._run_nodes_stage(request, final)
			stages = {name: StageOutcome(**data) for name, data in final["stages"].items()}
			for name in ("nodes", "main"):
				stages.setdefault(name, StageOutcome(name, "blocked"))
			workflow_id = final["workflow_id"]
			service = self.run_service(workflow_id) if request.serve and not final["error"] else None
			errors = dict(self.record(workflow_id).errors) if workflow_id else {"reformat": stages["reformat"].error}
			logs = {
				name: stage.artifacts[key]
				for name, stage, key in (
					("nodes_test", stages["nodes"], "test_logs"), ("nodes_audit", stages["nodes"], "audit_logs"),
					("main_run", stages["main"], "run_log"),
				) if key in stage.artifacts
			}
			return AgentResult(
				workflow_id=workflow_id, ok=not final["error"],
				intent=AgentIntent.from_dict(final["intent"]) if final["intent"] else None,
				stages=stages, errors=errors, logs=logs,
				artifacts=self.artifacts(workflow_id) if workflow_id else {}, service=service,
			)

	def _run_nodes_stage(self, request: AgentRequest, context: dict[str, Any]) -> dict[str, Any]:
		"""Run the nodes stage, which also runs the main entrypoint merged after it.

		``DynamicWorkflowOrchestrator`` owns this stage: its expanded graph runs every
		node chain, the transit exit step publishes the revision, and the continuation
		graph then runs ``main_entrypoint_generation``. The orchestrator reports the
		nodes and main stage records in ``last_stage_output``.
		"""
		record = self.record(context["workflow_id"])
		orchestrator = _orchestrator(record.workflow_id)
		intent = AgentIntent.from_dict(context["intent"])
		plan = intent.nodes
		skipped = (
			plan.mode == "skip"
			and not intent.node_amendments
			and not (request.cascade and _nodes_stale(record, orchestrator))
		)
		if not record.graph_path.is_file():
			message = f"workflow {record.workflow_id} has no graph; build it first"
			stage = {
				"stage": "nodes", "attempts": 0, "changed": False, "artifacts": {},
				"status": "skipped" if skipped else "failed",
				"error": "" if skipped else message,
			}
			if not skipped:
				record.errors["nodes"] = message
				record.save()
			return {
				**context,
				"stages": {**context["stages"], "nodes": stage},
				"error": context["error"] or stage["error"],
			}
		amendments = dict(intent.node_amendments)
		if plan.amendment:
			for name in _enabled_nodes(record.graph_path):
				amendments[name] = join_text(plan.amendment, amendments.get(name, ""))
		failures: dict[str, str] = {}

		def attempt(attempt_no: int, _failure: str) -> WorkflowVersion:
			# Failed nodes are amended from their own failure and logs; the rest is reused.
			retry = {
				name: join_text(amendments.get(name, ""), fix_text(text)) for name, text in failures.items()
			}
			try:
				return orchestrator.run(
					DynamicWorkflowRequest(
						workflow_json_path=record.graph_path, requirement_md_path=record.requirement_path,
						node_amendments={**amendments, **retry}, **request.node_options,
					),
					agent_state={
						"agent_run": _AgentRun(request, self),
						"agent_context": context,
						"agent_nodes_attempt": attempt_no,
						"agent_nodes_skipped": skipped,
					},
				)
			except DynamicWorkflowError as exc:
				failures.clear()
				failures.update({
					name: _failure_text(name, text, orchestrator.last_logs) for name, text in exc.failures.items()
				})
				raise

		version, attempts, error = _with_retries(request.retries("nodes"), attempt)
		record.clear_errors("nodes")
		if version is None:
			if failures:
				record.errors.update({error_key("nodes", name): text for name, text in failures.items()})
			else:
				record.errors["nodes"] = error
		record.save()
		last_stage = orchestrator.last_stage_output
		if last_stage is None:
			# An all-disabled graph publishes a snapshot and schedules nothing, so the
			# merged main-entrypoint stage never ran.
			stage = {
				"stage": "nodes", "attempts": attempts, "changed": False,
				"artifacts": {"workflow_version": version.version if version else 0},
				"status": "failed" if version is None else ("skipped" if skipped else "completed"),
				"error": error,
			}
			return {
				**context,
				"stages": {
					**context["stages"],
					"nodes": stage,
					"main": {
						"stage": "main", "status": "skipped", "attempts": 0,
						"error": "", "changed": False, "artifacts": {},
					},
				},
				"error": context["error"] or error,
			}
		return dict(last_stage)

	def build_workflow_json(self, prompt: str, *, workflow_id: str | None = None, **options: Any) -> AgentResult:
		"""Create (no ``workflow_id``) or modify workflow.json together with its requirement analysis."""
		intent = AgentIntent(
			workflow_id=workflow_id, requirement="" if workflow_id else prompt,
			graph=StagePlan("modify" if workflow_id else "create", prompt if workflow_id else ""),
		)
		return self.run(AgentRequest(intent=intent, **{"cascade": False, **options}))

	def build_nodes(
		self, workflow_id: str, amendments: Mapping[str, str] | None = None, **options: Any,
	) -> AgentResult:
		"""Build the workflow's nodes (only what changed), applying per-node amendments by step id."""
		intent = AgentIntent(
			workflow_id=workflow_id, nodes=StagePlan("modify" if amendments else "create"),
			node_amendments=dict(amendments or {}),
		)
		return self.run(AgentRequest(intent=intent, **{"cascade": False, **options}))

	def build_main_entrypoint(self, workflow_id: str, amendment: str = "", **options: Any) -> AgentResult:
		"""Create or regenerate the main entrypoint from the workflow's latest nodes."""
		mode = "modify" if self.record(workflow_id).main_entrypoint_path else "create"
		intent = AgentIntent(workflow_id=workflow_id, main=StagePlan(mode, amendment))
		return self.run(AgentRequest(intent=intent, **{"cascade": False, **options}))

	def build_workflow(self, prompt: str = "", *, workflow_id: str | None = None, **options: Any) -> AgentResult:
		"""Run the whole engine on a free-text request and/or logs; later stages follow earlier changes."""
		return self.run(AgentRequest(prompt=prompt, workflow_id=workflow_id, **options))

	def evolve(
		self, workflow_id: str, command: str, *, target: str | None = None, **options: Any,
	) -> AgentResult:
		"""Evolve ``workflow_id`` by ``command``: all of it (``target="workflow"``), one node (its step id or
		node id), or, without ``target``, whatever the judge decides the command refers to."""
		if not command.strip():
			raise ValueError("command must be a non-empty string")
		return self.run(AgentRequest(prompt=command, workflow_id=workflow_id, target=target, **options))

	def run_service(self, workflow_id: str) -> dict[str, Any]:
		"""Start the workflow's main entrypoint as a service, replacing a previous run of it."""
		record = self.record(workflow_id)
		path = record.main_entrypoint_path
		if path is None or not path.is_file():
			raise FileNotFoundError(f"workflow {workflow_id} has no main entrypoint; build it first")
		record.process, info = self.runtime().rerun_server(
			main_entrypoint_path=str(path), previous_process=record.process,
		)
		return {
			"workflow_id": workflow_id,
			"main_entrypoint_id": record.main_entrypoint_id,
			"base_url": record.base_url,
			**info,
		}

	def stop_service(self, workflow_id: str) -> None:
		record = self.record(workflow_id)
		process, record.process = record.process, None
		if process is not None:
			self.runtime().stop_managed_server_process(process)

	def _stop_services(self) -> None:
		for workflow_id in self.workflow_ids:
			self.stop_service(workflow_id)

	def _unregister(self) -> None:
		with self._registry_lock:
			ids, self._records = list(self._records), {}
		with ExitStack() as cleanup:
			for workflow_id in ids:
				with _ORCHESTRATORS_LOCK:
					orchestrator = ORCHESTRATORS.pop(workflow_id, None)
				if orchestrator is not None:
					cleanup.callback(orchestrator.close)

	def close(self) -> None:
		"""Stop services started here, release the native engine and the orchestrators."""
		with self._run_lock:
			if self._closed:
				return
			self._closed = True
			engine, self._engine = self._engine, None
			with ExitStack() as cleanup:
				cleanup.callback(self._unregister)
				if engine is not None:
					cleanup.callback(close_workflow_engine, engine)
				if self._runtime is not None:
					cleanup.callback(self._stop_services)


__all__ = [
	"AgentRequest", "AgentResult", "AgentWorkflowEngine", "StageOutcome", "WorkflowRecord",
]
