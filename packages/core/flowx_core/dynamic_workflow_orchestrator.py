# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Versioned, dependency-ordered node generation through one expanded business engine.

Modified 2026-10-15: add persisted identities, immutable artifact snapshots and retention.
Modified 2026-10-15: schedule generation nodes with JSON-built WorkflowEngine graphs.
Modified 2026-10-16: native steps run generation and repair through AgentBuilder-published workflows.
Modified 2026-10-17: repair a node again, up to max_repair_retries times, until its tests and audit pass.
Modified 2026-10-20: ask for user repair guidance after the node test result is available.
Modified 2026-10-28: expand each business node into prefixed generation/guard/repair/audit steps
inside one engine, with per-node request namespaces and loop-policy bounded repair.
Modified 2026-10-30: the orchestrator is the expanded engine itself; a transit
``nodes_stage_exit`` step publishes the run, and an optional continuation graph (the agent
engine's main-entrypoint stage) is merged after every leaf finalizer.
This coordinator does not execute the generated business graph or implement a ReAct judge.
"""

from __future__ import annotations

import json
import keyword
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from ag_ui_workflow import (
	GraphSpec,
	StepRunOutput,
	WorkflowStepNode,
	merge_graphs,
	rebind_step_class,
)

from flowx_core.tools.artifact_tools import (
	fingerprint_json,
	write_json_atomic,
)
from flowx_core.tools.workflow_engine import close_workflow_engine
from flowx_core.tools.workflow_logs import WorkflowNodeLogs
from flowx_core.workflows.node_generation import NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import NodeRuntimeModifierPipeline
from flowx_core.workflows.dynamic_node_graph import (
	NAMESPACE_KEY,
	ExpandedWorkflowEngine,
	NodeRepairInput,
)


@dataclass(frozen=True, kw_only=True)
class DynamicWorkflowRequest:
	"""Build a JSON graph revision; state is run-local, never persisted in history.

	State-only changes require ``force_nodes`` or explicit node amendments. Global
	requirement changes conservatively rebuild all enabled nodes. Generated nodes
	currently use the existing Python generation/audit/runtime contracts.

	Each changed node runs node generation (``run_node_generation``) and then the
	runtime modifier (``run_runtime_modifier``). Disabling generation repairs the
	node's latest saved source and rejects amendments, which only generation applies.

	A repair that fails (tests still failing after an amendment, or an audit violation) is
	requested again up to ``max_repair_retries`` more times; 0 restores a single pass.

	``repair_input_callback`` receives each node's fresh failed test result and returns
	guidance appended to that test log, ``skip`` to skip the amendment, or an empty
	string to amend from the test log alone.
	"""

	workflow_json_path: str | Path
	requirement_md_path: str | Path
	node_amendments: Mapping[str, str] = field(default_factory=dict)
	markdown_amendments: Mapping[str, str] = field(default_factory=dict)
	force_nodes: tuple[str, ...] = ()
	node_formats: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
	generate_markdown: bool = True
	skip_node_planner: bool = False
	run_node_generation: bool = True
	run_runtime_modifier: bool = True
	temperature: float = 0.35
	test_temperature: float = 0.2
	amendment_temperature: float = 0.3
	python_command: str = ""
	timeout: int = 60
	max_repair_retries: int = 2
	repair_input_callback: Callable[[NodeRepairInput], str] | None = None
	state: Mapping[str, Any] = field(default_factory=dict)

	def __post_init__(self) -> None:
		retries = self.max_repair_retries
		if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
			raise ValueError("max_repair_retries must be a non-negative integer")
		if self.repair_input_callback is not None and not callable(self.repair_input_callback):
			raise ValueError("repair_input_callback must be callable or None")


@dataclass(frozen=True)
class NodeVersion:
	"""One node identity/version and its saved source, documentation and artifacts."""

	node_id: str
	node_name: str
	version: int
	input_fingerprint: str
	artifacts: Mapping[str, Path]

	@property
	def source_path(self) -> Path:
		return self.artifacts["source"]

	@property
	def markdown_path(self) -> Path | None:
		return self.artifacts.get("markdown")


@dataclass(frozen=True)
class WorkflowVersion:
	"""Self-contained graph snapshot pinning every enabled node's exact version."""

	workflow_id: str
	version: int
	workflow_path: Path
	requirement_md_path: Path
	node_versions: Mapping[str, NodeVersion]
	changed_nodes: tuple[str, ...]

	@property
	def generated_node_id_map(self) -> dict[str, str]:
		return {name: node.node_id for name, node in self.node_versions.items()}


class DynamicWorkflowError(RuntimeError):
	"""A run failed; ``failures`` maps each node that failed itself (not blocked ones) to its error."""

	def __init__(self, message: str, failures: Mapping[str, str]) -> None:
		super().__init__(message)
		self.failures = dict(failures)


# Published by AgentBuilder: class-only native steps cannot take constructor arguments.
NODE_GENERATION_WORKFLOW: NodeGenerationWorkflow | None = None
NODE_RUNTIME_MODIFIER_PIPELINE: NodeRuntimeModifierPipeline | None = None


def _published_workflows() -> tuple[NodeGenerationWorkflow, NodeRuntimeModifierPipeline]:
	if NODE_GENERATION_WORKFLOW is None or NODE_RUNTIME_MODIFIER_PIPELINE is None:
		raise RuntimeError("AgentBuilder has not published its node generation workflow and runtime modifier")
	return NODE_GENERATION_WORKFLOW, NODE_RUNTIME_MODIFIER_PIPELINE


NODES_STAGE_EXIT_ID = "nodes_stage_exit"


@dataclass(frozen=True)
class NodesStageOutcome:
	"""What the transit exit step published for one nodes run."""

	status: str  # "completed" or "failed"
	version: WorkflowVersion | None = None
	error: str = ""
	failures: Mapping[str, str] = field(default_factory=dict)  # node name -> root cause


@dataclass(frozen=True, kw_only=True)
class _GenerationRun:
	"""Inputs attached to one native run, never retained by pooled step instances."""

	request: DynamicWorkflowRequest
	directory: Path
	graph: Mapping[str, Any]
	order: tuple[str, ...]
	version: int
	requirement_text: str
	raw_nodes: Mapping[str, Mapping[str, Any]]
	artifacts: Mapping[str, Mapping[str, Path]]
	registry: Mapping[str, Any]
	generation: NodeGenerationWorkflow
	modifier: NodeRuntimeModifierPipeline


class _NodesStageExitStep(WorkflowStepNode):
	"""Transit join: publish the node chains, then hand the nodes stage to what follows.

	``ExpandedWorkflowEngine`` bridges this step after every leaf finalizer, so it runs
	only once all node chains finished. The revision is published here rather than after
	the run because a continuation stage (for example the agent's main entrypoint)
	reads the published workflow version inside the same native run.
	"""

	INPUT_REQUIRED = False
	STEP_ID = NODES_STAGE_EXIT_ID
	TITLE = "Publish nodes"
	DEPENDENCIES: list[str] = []

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		orchestrator = session_state.get("dynamic_workflow_orchestrator")
		run = session_state.get("dynamic_workflow_run")
		if not isinstance(orchestrator, DynamicWorkflowOrchestrator) or not isinstance(run, _GenerationRun):
			return StepRunOutput(
				card={"status": "failed"},
				derived={"error": "nodes stage state is missing"},
			)
		context = dict(session_state.get("agent_context") or {})
		if not context:
			# Single connected pass: the head ran inside this same graph.
			head_id = orchestrator.head_terminal_id
			head_output = self.get_workflow_param().outputs.get(head_id) if head_id else None
			context = dict(head_output.derived) if head_output is not None else {}
		logs = session_state.get("dynamic_workflow_logs")
		artifacts: dict[str, Any] = {
			"test_logs": dict(logs.test) if isinstance(logs, WorkflowNodeLogs) else {},
			"audit_logs": dict(logs.audit) if isinstance(logs, WorkflowNodeLogs) else {},
		}
		outcome = orchestrator.finish_nodes_run(run, self.get_workflow_param().outputs)
		if outcome.status == "failed":
			stage = {
				"stage": "nodes",
				"status": "failed",
				"attempts": int(session_state.get("agent_nodes_attempt", 1)),
				"error": outcome.error,
				"changed": False,
				"artifacts": artifacts,
			}
			error = context.get("error") or outcome.error
		else:
			version = outcome.version
			artifacts.update(
				workflow_version=version.version if version else 0,
				changed_nodes=list(version.changed_nodes) if version else [],
				node_ids=version.generated_node_id_map if version else {},
			)
			stage = {
				"stage": "nodes",
				"status": "skipped" if session_state.get("agent_nodes_skipped") else "completed",
				"attempts": int(session_state.get("agent_nodes_attempt", 1)),
				"error": "",
				"changed": version is not None and version.version == run.version,
				"artifacts": artifacts,
			}
			error = context.get("error") or ""
		return StepRunOutput(
			card={"stage": "nodes", "status": stage["status"]},
			derived={
				**context,
				"stages": {**context.get("stages", {}), "nodes": stage},
				"error": error,
			},
		)


class DynamicWorkflowOrchestrator(ExpandedWorkflowEngine):
	"""Version and publish the nodes of one persisted workflow ID.

	This orchestrator **is** the expanded engine: the first nonempty run builds the
	merged business-node graph through ``_init_expanded``, every later run diffs the
	business DAG in place through ``update``, and a transit ``nodes_stage_exit`` step
	publishes the revision. ``continuation`` optionally appends a graph after that
	join; the agent engine merges its main-entrypoint stage there, so one native run
	covers node generation, publication and that stage.

	``storage_dir`` belongs to one workflow. IDs survive reload, deletion and
	re-addition of the same node name; renaming a node is currently remove+add.
	``keep_last_versions=None`` retains all history; a positive N retains the
	last N workflow revisions and, independently, the last N revisions per node.
	Workflow bundles contain copies, so node-history pruning cannot break them.
	Each expanded step reads its own node namespace, so independent business
	branches cannot overwrite another node's request state.

	Runs on this instance are serialized. A second writer encountering an existing
	candidate directory fails rather than overwriting it. This is not a distributed
	revision store.
	"""

	REQUEST_TYPES = (DynamicWorkflowRequest,)
	CLOSED_MESSAGE = "dynamic workflow orchestrator is closed"

	def __init__(
		self,
		*,
		storage_dir: str | Path,
		workflow_id: str | None = None,
		keep_last_versions: int | None = None,
		continuation: GraphSpec | None = None,
		head: GraphSpec | None = None,
	) -> None:
		if keep_last_versions is not None and (
			isinstance(keep_last_versions, bool)
			or not isinstance(keep_last_versions, int)
			or keep_last_versions < 1
		):
			raise ValueError("keep_last_versions must be a positive integer or None")
		if workflow_id is not None and (not isinstance(workflow_id, str) or not workflow_id.strip()):
			raise ValueError("workflow_id must be a non-empty string")
		self.storage_dir = Path(storage_dir).expanduser().resolve()
		self.keep_last_versions = keep_last_versions
		# Not WorkflowEngine._lock, which guards the native run registry; reads below
		# happen from pooled worker threads during a run that already holds this one.
		self._run_lock = RLock()
		self._closed = False
		self._engine_ready = False
		self._head = head
		self._tail, self._terminal_id = self._build_tail(continuation)
		self._last_logs: WorkflowNodeLogs | None = None
		self._last_stage_output: Mapping[str, Any] | None = None
		self._nodes_outcome: NodesStageOutcome | None = None
		self.storage_dir.mkdir(parents=True, exist_ok=True)
		self._manifest_path = self.storage_dir / "manifest.json"
		if self._manifest_path.is_file():
			manifest = self._read_manifest()
			if workflow_id is not None and workflow_id != manifest["workflow_id"]:
				raise ValueError("storage_dir already belongs to a different workflow_id")
		else:
			manifest = {
				"schema_version": 1,
				"workflow_id": workflow_id or uuid.uuid4().hex,
				"workflow_version": 0,
				"workflows": [],
				"nodes": {},
			}
			self._write_json(self._manifest_path, manifest)
		self._workflow_id: str = manifest["workflow_id"]
		if self.keep_last_versions is not None:
			self.prune_versions()

	@staticmethod
	def _build_tail(continuation: GraphSpec | None) -> tuple[GraphSpec, str]:
		"""The transit exit step plus any continuation graph merged after it.

		The returned spec ends with the continuation's terminal step, which is the
		stage the orchestrator reports as its last output.
		"""
		exit_spec = GraphSpec(
			nodes=[{"name": NODES_STAGE_EXIT_ID}],
			classes={NODES_STAGE_EXIT_ID: _NodesStageExitStep},
			steps_meta=[_NodesStageExitStep.step_meta()],
		)
		if continuation is None or not continuation.nodes:
			return exit_spec, NODES_STAGE_EXIT_ID
		roots = [node["name"] for node in continuation.nodes if not node.get("depends")]
		if not roots:
			return exit_spec, NODES_STAGE_EXIT_ID
		merged = merge_graphs(
			exit_spec,
			continuation,
			bridges={root: [NODES_STAGE_EXIT_ID] for root in roots},
		)
		classes = dict(merged.classes)
		for root in roots:
			classes[root] = rebind_step_class(
				continuation.classes[root], dependencies=[NODES_STAGE_EXIT_ID],
			)
		tail = GraphSpec(
			nodes=merged.nodes,
			classes=classes,
			steps_meta=merged.steps_meta,
			loop=merged.loop,
			loop_key=merged.loop_key,
			loop_policies=merged.loop_policies,
			loop_bindings=merged.loop_bindings,
			hidden_steps=merged.hidden_steps,
		)
		parents = {dep for node in tail.nodes for dep in node.get("depends", [])}
		terminal = [node["name"] for node in tail.nodes if node["name"] not in parents]
		return tail, terminal[-1] if terminal else NODES_STAGE_EXIT_ID

	def _read_manifest(self) -> dict[str, Any]:
		manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
		if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
			raise ValueError("unsupported dynamic workflow manifest")
		return manifest

	_write_json = staticmethod(write_json_atomic)

	@property
	def workflow_id(self) -> str:
		return self._workflow_id

	@property
	def workflow_version(self) -> int:
		return self._read_manifest()["workflow_version"]

	@property
	def generated_node_id_map(self) -> dict[str, str]:
		workflows = self._read_manifest()["workflows"]
		return {name: node["node_id"] for name, node in workflows[-1]["nodes"].items()} if workflows else {}

	@property
	def generation_engine(self) -> "DynamicWorkflowOrchestrator" | None:
		"""This orchestrator once its expanded graph is built, else ``None``."""
		return self if self._engine_ready else None

	@property
	def last_logs(self) -> WorkflowNodeLogs | None:
		"""Test and audit log text by node step_id from the latest run, including a failed one."""
		return self._last_logs

	@property
	def last_stage_output(self) -> Mapping[str, Any] | None:
		"""The terminal step's derived context from the latest run.

		With a continuation that is the appended stage's output (the agent's main
		entrypoint); without one it is the transit exit step's nodes-stage record.
		"""
		return self._last_stage_output

	@property
	def head_step_ids(self) -> tuple[str, ...]:
		"""Step ids of the connected head graph, or empty without one."""
		return tuple(node["name"] for node in self._head.nodes) if self._head is not None else ()

	@property
	def head_terminal_id(self) -> str:
		"""The head step that feeds every business root entry, or an empty string."""
		if self._head is None or not self._head.nodes:
			return ""
		parents = {dep for node in self._head.nodes for dep in node.get("depends", [])}
		terminals = [node["name"] for node in self._head.nodes if node["name"] not in parents]
		return terminals[-1] if terminals else ""

	def _body_step_ids(self) -> tuple[str, ...]:
		"""Every scheduled step except the head, for a head-only pass."""
		head = set(self.head_step_ids)
		return tuple(step["id"] for step in self.steps_meta if step["id"] not in head)

	@staticmethod
	def _expanded_pool_size(
		graph: Mapping[str, Any],
		generation: NodeGenerationWorkflow,
		modifier: NodeRuntimeModifierPipeline,
	) -> int:
		return max(
			int(graph.get("pool_size", 1)),
			int(generation.json_config().get("pool_size", 1)),
			int(modifier.json_config().get("pool_size", 1)),
		)

	def _ensure_engine(
		self,
		*,
		business_nodes: list[dict[str, Any]],
		generation: NodeGenerationWorkflow,
		modifier: NodeRuntimeModifierPipeline,
		pool_size: int,
	) -> None:
		"""Build the expanded graph on first use, then diff the business DAG in place."""
		if not self._engine_ready:
			if getattr(generation, "graph", None) is None or getattr(modifier, "graph", None) is None:
				raise RuntimeError(
					"dynamic workflow generation requires graph-backed node generation and runtime modifier workflows"
				)
			ExpandedWorkflowEngine._init_expanded(
				self,
				business_nodes=business_nodes,
				generation=generation,
				modifier=modifier,
				pool_size=pool_size,
				graph_config_id=f"{self.workflow_id}:expanded",
				continuation=self._tail,
				head=self._head,
			)
			self._engine_ready = True
			return
		self.update(
			business_nodes=business_nodes,
			generation=generation,
			modifier=modifier,
			pool_size=pool_size,
		)

	def run(self, request: Any, *args: Any, **kwargs: Any) -> Any:
		"""Publish a revision for a ``DynamicWorkflowRequest``; keep the native run form working."""
		if not isinstance(request, self.REQUEST_TYPES) and not self._engine_ready:
			raise RuntimeError("dynamic workflow has no scheduled graph yet; pass a DynamicWorkflowRequest")
		return super().run(request, *args, **kwargs)

	def _saved_path(self, relative: str) -> Path:
		path = (self.storage_dir / relative).resolve()
		if not path.is_relative_to(self.storage_dir):
			raise ValueError("artifact path escapes storage_dir")
		return path

	def _node_version(self, record: Mapping[str, Any]) -> NodeVersion:
		return NodeVersion(
			node_id=record["node_id"], node_name=record["node_name"],
			version=record["version"], input_fingerprint=record["input_fingerprint"],
			artifacts={key: self._saved_path(value) for key, value in record["artifacts"].items()},
		)

	def _workflow_version(self, record: Mapping[str, Any]) -> WorkflowVersion:
		return WorkflowVersion(
			workflow_id=self.workflow_id, version=record["version"],
			workflow_path=self._saved_path(record["workflow_path"]),
			requirement_md_path=self._saved_path(record["requirement_md_path"]),
			node_versions={name: self._node_version(node) for name, node in record["nodes"].items()},
			changed_nodes=tuple(record["changed_nodes"]),
		)

	def list_workflow_versions(self) -> tuple[WorkflowVersion, ...]:
		return tuple(self._workflow_version(item) for item in self._read_manifest()["workflows"])

	def get_workflow_version(self, version: int | None = None) -> WorkflowVersion:
		versions = self.list_workflow_versions()
		for item in reversed(versions):
			if version is None or item.version == version:
				return item
		raise KeyError(f"workflow version not retained: {version}")

	def list_node_versions(self, node_id: str) -> tuple[NodeVersion, ...]:
		for node in self._read_manifest()["nodes"].values():
			if node["node_id"] == node_id:
				return tuple(self._node_version(item) for item in node["versions"])
		raise KeyError(f"unknown node_id: {node_id}")

	def get_node_version(self, node_id: str, version: int | None = None) -> NodeVersion:
		for item in reversed(self.list_node_versions(node_id)):
			if version is None or item.version == version:
				return item
		raise KeyError(f"node version not retained: {node_id}/{version}")

	@staticmethod
	def _load_graph(path: str | Path) -> tuple[dict[str, Any], list[str]]:
		graph = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
		if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or not graph["nodes"]:
			raise ValueError("workflow JSON requires a non-empty nodes list")
		pool_size = graph.get("pool_size", 1)
		if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1:
			raise ValueError("workflow pool_size must be a positive integer")
		dependencies: dict[str, list[str]] = {}
		enabled: set[str] = set()
		filenames: set[str] = set()
		reserved = {name.casefold() for name in sys.stdlib_module_names} | {
			"pytest", "conftest", "ag_ui_workflow", "pydaograph", "flowx_core",
		}
		for node in graph["nodes"]:
			if not isinstance(node, dict):
				raise ValueError("workflow nodes must be JSON objects")
			name = node.get("name")
			if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or keyword.iskeyword(name):
				raise ValueError("node names must be safe Python identifiers")
			if name in dependencies:
				raise ValueError(f"duplicate workflow node: {name}")
			if name.casefold() in filenames:
				raise ValueError(f"node names collide on case-insensitive storage: {name}")
			if name.casefold() in reserved:
				raise ValueError(f"node name shadows a runtime or test-runner module: {name}")
			filenames.add(name.casefold())
			depends = node.get("depends", [])
			if not isinstance(depends, list) or any(not isinstance(dep, str) for dep in depends):
				raise ValueError(f"node {name} depends must be a list of names")
			if len(set(depends)) != len(depends) or name in depends:
				raise ValueError(f"node {name} has duplicate or self dependencies")
			if not isinstance(node.get("enable", True), bool):
				raise ValueError(f"node {name} enable must be a boolean")
			if node.get("loop", 1) != 1:
				raise ValueError("generation nodes must be single-pass (loop=1)")
			dependencies[name] = depends
			if node.get("enable", True):
				enabled.add(name)
		for name, depends in dependencies.items():
			if set(depends).difference(dependencies):
				raise ValueError(f"node {name} has undefined dependencies")
			if name in enabled and set(depends).difference(enabled):
				raise ValueError(f"enabled node {name} depends on a disabled node")
		# CycleError is a ValueError; validate even disabled parts of the graph.
		order = list(TopologicalSorter(dependencies).static_order())
		return graph, [name for name in order if name in enabled]

	@staticmethod
	def _artifact_targets(directory: Path, name: str) -> dict[str, Path]:
		return {
			"source": directory / f"{name}.py",
			"markdown": directory / "node_docs" / f"{name}.md",
			"test": directory / "tests" / f"test_{name}.py",
			"log": directory / "logs" / f"{name}_test.log",
			"repair_log": directory / "logs" / f"{name}_repair.log",
		}

	def _record_node(
		self, node_id: str, name: str, version: int, fingerprint: str, artifacts: Mapping[str, Path],
	) -> dict[str, Any]:
		return {
			"node_id": node_id, "node_name": name, "version": version,
			"input_fingerprint": fingerprint,
			"artifacts": {key: str(path.relative_to(self.storage_dir)) for key, path in artifacts.items() if path.is_file()},
		}

	@staticmethod
	def _copy_artifacts(source: Mapping[str, Path], target: Mapping[str, Path]) -> None:
		for key, path in source.items():
			target[key].parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(path, target[key])

	def execute(
		self,
		request: DynamicWorkflowRequest,
		agent_state: Mapping[str, Any] | None = None,
	) -> WorkflowVersion | None:
		"""Publish a complete revision, or return the existing revision for a no-op.

		The connected graph runs the optional head, every business node chain and then
		the transit exit step, which publishes this revision. ``agent_state`` is seeded
		into the native state: the agent run payload, the previous stage's context, and
		the flags that select the pass layout:

		``agent_head_done``
			The head already ran (the agent's shared head engine resolved which
			workflow this request targets); only the body runs and the head steps are
			skipped.
		``agent_graph_stable``
			The head cannot rewrite ``workflow.json`` in this request, so the connected
			graph runs once with no skips.
		``agent_nodes_skipped``
			The nodes stage is reported as skipped. The chains still run in reuse mode,
			because the main-entrypoint continuation may need their published revision;
			the agent skips the whole body when both stages are skipped.

		Source is included in child generation context, so a changed parent source
		invalidates its children. No requests, native nodes or state dictionaries
		are retained. Failed candidates do not change IDs, counters or current head.
		"""
		with self._run_lock:
			if self._closed:
				raise RuntimeError(self.CLOSED_MESSAGE)
			generation, modifier = _published_workflows()
			if not (request.run_node_generation or request.run_runtime_modifier):
				raise ValueError("enable node generation, the runtime modifier, or both")
			if not request.run_node_generation and (request.node_amendments or request.markdown_amendments):
				raise ValueError("node and markdown amendments require node generation")
			graph_path = Path(request.workflow_json_path).expanduser().resolve()
			requirement_path = Path(request.requirement_md_path).expanduser().resolve()
			state_extra = dict(agent_state or {})
			head_done = bool(state_extra.get("agent_head_done"))
			graph_stable = bool(state_extra.get("agent_graph_stable"))
			graph, order = self._load_business(graph_path)
			self._last_logs = WorkflowNodeLogs()
			self._last_stage_output = None
			self._nodes_outcome = None

			if not head_done and self._head is not None and order and not graph_stable:
				# The graph stage may rewrite workflow.json, and the chains were built
				# from the previous revision, so this pass runs the head steps only.
				head_state = {
					key: value for key, value in state_extra.items()
					if key not in ("agent_context", "agent_graph_stable")
				}
				head_result = self.run_steps(
					state=head_state,
					skipped_steps=self._body_step_ids(),
				)
				self._last_stage_output = dict(head_result.outputs[self.head_terminal_id].derived)
				if self._last_stage_output.get("error"):
					raise RuntimeError(self._last_stage_output["error"])
				head_done = True
				graph, order = self._load_business(graph_path)

			requirement_text = requirement_path.read_text(encoding="utf-8") if requirement_path.is_file() else ""
			selected = set(request.force_nodes) | set(request.node_amendments) | set(request.markdown_amendments) | set(request.node_formats)
			if selected.difference(order):
				raise ValueError(f"unknown or disabled requested nodes: {sorted(selected.difference(order))}")
			# Sorted so that reordering the JSON does not reload the pool.
			generation_config = {
				"pool_size": graph.get("pool_size", 1),
				"nodes": sorted(
					({"name": node["name"], "depends": sorted(node.get("depends", []))}
					for node in graph["nodes"] if node.get("enable", True)),
					key=lambda node: node["name"],
				),
			}
			business_nodes = generation_config["nodes"]
			if business_nodes:
				self._ensure_engine(
					business_nodes=business_nodes,
					generation=generation,
					modifier=modifier,
					pool_size=self._expanded_pool_size(graph, generation, modifier),
				)
			manifest = self._read_manifest()
			version = manifest["workflow_version"] + 1
			directory = self.storage_dir / "workflows" / f"{version:08d}"
			# Never overwrite another writer's candidate or an interrupted build.
			directory.mkdir(parents=True, exist_ok=False)
			logs = self._last_logs
			try:
				self._write_json(directory / "workflow.json", graph)
				self._write_json(directory / "generation_workflow.json", generation_config)
				(directory / "requirement_analysis.md").write_text(requirement_text, encoding="utf-8")
				raw_nodes = {node["name"]: node for node in graph["nodes"]}
				artifacts: dict[str, dict[str, Path]] = {}
				# Make all prior sources available for graph-aware ancestor inspection.
				for name in order:
					registry = manifest["nodes"].get(name)
					targets = self._artifact_targets(directory, name)
					artifacts[name] = targets
					if registry:
						self._copy_artifacts(self._node_version(registry["versions"][-1]).artifacts, targets)
				run = _GenerationRun(
					request=request, directory=directory, graph=graph,
					order=tuple(order), version=version,
					requirement_text=requirement_text, raw_nodes=raw_nodes,
					artifacts=artifacts, registry=manifest["nodes"],
					generation=generation, modifier=modifier,
				)
				if not business_nodes:
					return self._publish_empty_graph(run, manifest)
				skipped: list[str] = []
				if head_done:
					skipped.extend(self.head_step_ids)
				result = self.run_steps(
					state={
						"dynamic_workflow_run": run,
						"dynamic_workflow_orchestrator": self,
						"dynamic_workflow_logs": logs,
						NAMESPACE_KEY: {},
						"dynamic_workflow_namespace_lock": RLock(),
						**state_extra,
					},
					skipped_steps=skipped,
				)
				self._last_stage_output = dict(result.outputs[self._terminal_id].derived)
			except BaseException:
				if self._nodes_outcome is None:
					shutil.rmtree(directory, ignore_errors=True)
				raise
			outcome = self._nodes_outcome
			if outcome is None:
				raise RuntimeError("nodes stage did not publish an outcome")
			if outcome.status == "failed":
				raise DynamicWorkflowError(outcome.error, outcome.failures)
			return outcome.version

	@staticmethod
	def _load_business(graph_path: Path) -> tuple[dict[str, Any], list[str]]:
		"""The workflow JSON and its enabled node order, or ``({}, [])`` when absent."""
		if not graph_path.is_file():
			return {}, []
		return DynamicWorkflowOrchestrator._load_graph(graph_path)

	def _publish_empty_graph(
		self, run: _GenerationRun, manifest: dict[str, Any],
	) -> WorkflowVersion:
		"""Publish a snapshot for an all-disabled business graph; nothing is scheduled."""
		graph_fingerprint = fingerprint_json({"graph": run.graph, "requirement": run.requirement_text})
		if manifest["workflows"] and manifest["workflows"][-1]["input_fingerprint"] == graph_fingerprint:
			shutil.rmtree(run.directory, ignore_errors=True)
			return self._workflow_version(manifest["workflows"][-1])
		directory = run.directory
		workflow_record = {
			"workflow_id": self.workflow_id,
			"version": run.version, "input_fingerprint": graph_fingerprint,
			"workflow_path": str((directory / "workflow.json").relative_to(self.storage_dir)),
			"generation_workflow_path": str((directory / "generation_workflow.json").relative_to(self.storage_dir)),
			"requirement_md_path": str((directory / "requirement_analysis.md").relative_to(self.storage_dir)),
			"nodes": {}, "changed_nodes": [],
		}
		try:
			self._write_json(directory / "version.json", workflow_record)
			manifest["workflow_version"] = run.version
			manifest["workflows"].append(workflow_record)
			removed = self._trim_history(manifest)
			self._write_json(self._manifest_path, manifest)
		except BaseException:
			shutil.rmtree(directory, ignore_errors=True)
			raise
		for path in removed:
			shutil.rmtree(path, ignore_errors=True)
		return self._workflow_version(workflow_record)

	def finish_nodes_run(
		self,
		run: _GenerationRun,
		outputs: Mapping[str, StepRunOutput],
	) -> NodesStageOutcome:
		"""Publish or discard one nodes run; called by the transit exit step mid-run.

		It executes on a pooled worker thread while ``execute`` holds the run lock,
		so it must not acquire that lock.
		"""
		output_names = self.finalize_ids
		failed = {
			name: outputs[output_names[name]].derived["error"]
			for name in run.order if outputs[output_names[name]].derived.get("error")
		}
		if failed:
			failures = {
				name: error for name, error in failed.items()
				if outputs[output_names[name]].card.get("status") == "failed"
			}
			shutil.rmtree(run.directory, ignore_errors=True)
			self._nodes_outcome = NodesStageOutcome(
				"failed",
				error="dynamic workflow generation failed:\n"
				+ "\n".join(f"{name}: {error}" for name, error in failed.items()),
				failures=failures,
			)
			return self._nodes_outcome
		self._nodes_outcome = NodesStageOutcome("completed", self._publish_nodes(run, outputs))
		return self._nodes_outcome

	def _publish_nodes(
		self, run: _GenerationRun, outputs: Mapping[str, StepRunOutput],
	) -> WorkflowVersion:
		"""Record changed node versions and append this run as a workflow revision."""
		manifest = self._read_manifest()
		directory = run.directory
		output_names = self.finalize_ids
		created_node_dirs: list[Path] = []
		try:
			node_records: dict[str, Any] = {}
			changed: list[str] = []
			for name in run.order:
				output = outputs[output_names[name]].derived
				node_record = self._record_node(
					output["node_id"], name, output["version"], output["input_fingerprint"], run.artifacts[name],
				)
				node_records[name] = node_record
				if not output["changed"]:
					continue
				registry = manifest["nodes"].setdefault(name, {
					"node_id": node_record["node_id"], "version": 0, "versions": [],
				})
				registry["version"] = node_record["version"]
				node_dir = self.storage_dir / "nodes" / registry["node_id"] / f"{registry['version']:08d}"
				node_dir.mkdir(parents=True, exist_ok=False)
				created_node_dirs.append(node_dir)
				node_targets = self._artifact_targets(node_dir, name)
				self._copy_artifacts(self._node_version(node_record).artifacts, node_targets)
				saved_record = self._record_node(
					registry["node_id"], name, registry["version"], node_record["input_fingerprint"], node_targets,
				)
				registry["versions"].append(saved_record)
				self._write_json(node_dir / "version.json", saved_record)
				changed.append(name)
			graph_fingerprint = fingerprint_json({"graph": run.graph, "requirement": run.requirement_text})
			if not changed and manifest["workflows"] and manifest["workflows"][-1]["input_fingerprint"] == graph_fingerprint:
				shutil.rmtree(directory, ignore_errors=True)
				return self._workflow_version(manifest["workflows"][-1])
			workflow_record = {
				"workflow_id": self.workflow_id,
				"version": run.version, "input_fingerprint": graph_fingerprint,
				"workflow_path": str((directory / "workflow.json").relative_to(self.storage_dir)),
				"generation_workflow_path": str((directory / "generation_workflow.json").relative_to(self.storage_dir)),
				"requirement_md_path": str((directory / "requirement_analysis.md").relative_to(self.storage_dir)),
				"nodes": node_records, "changed_nodes": changed,
			}
			self._write_json(directory / "version.json", workflow_record)
			manifest["workflow_version"] = run.version
			manifest["workflows"].append(workflow_record)
			removed = self._trim_history(manifest)
			self._write_json(self._manifest_path, manifest)
			for path in removed:
				shutil.rmtree(path, ignore_errors=True)
			return self._workflow_version(workflow_record)
		except BaseException:
			shutil.rmtree(directory, ignore_errors=True)
			for node_dir in created_node_dirs:
				shutil.rmtree(node_dir, ignore_errors=True)
			raise

	def _trim_history(self, manifest: dict[str, Any]) -> list[Path]:
		removed: list[Path] = []
		limit = self.keep_last_versions
		if limit is None:
			return removed
		histories = [manifest["workflows"], *(node["versions"] for node in manifest["nodes"].values())]
		for history in histories:
			expired = history[:-limit]
			del history[:-limit]
			for record in expired:
				path = record.get("workflow_path") or record["artifacts"]["source"]
				removed.append(self._saved_path(path).parent)
		return removed

	def prune_versions(self) -> None:
		"""Apply this instance's retention policy without building a new version."""
		with self._run_lock:
			if self._closed:
				raise RuntimeError(self.CLOSED_MESSAGE)
			manifest = self._read_manifest()
			removed = self._trim_history(manifest)
			self._write_json(self._manifest_path, manifest)
			for path in removed:
				shutil.rmtree(path, ignore_errors=True)

	def close(self, request_id: str | None = None) -> None:
		"""Abandon one native run, or release the expanded engine.

		``AgentBuilder`` closes the published child workflows. Safe before the first
		run, when no pool exists yet; terminal and idempotent otherwise.
		"""
		if request_id is not None:
			super().close(request_id)
			return
		with self._run_lock:
			if self._closed:
				return
			self._closed = True
			if self._engine_ready:
				close_workflow_engine(self)


__all__ = [
	"DynamicWorkflowError", "DynamicWorkflowOrchestrator", "DynamicWorkflowRequest",
	"NODES_STAGE_EXIT_ID", "NodeRepairInput", "NodesStageOutcome", "NodeVersion", "WorkflowVersion",
]
