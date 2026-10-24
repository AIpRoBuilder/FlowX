# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Versioned, dependency-ordered node generation through two reusable workflows.

Modified 2026-10-15: add persisted identities, immutable artifact snapshots and retention.
Modified 2026-10-15: schedule generation nodes with JSON-built WorkflowEngine graphs.
Modified 2026-10-16: native steps run generation and repair through AgentBuilder-published workflows;
one engine reloads its pool when the workflow config changes.
Modified 2026-10-17: repair a node again, up to max_repair_retries times, until its tests and audit pass.
Modified 2026-10-20: ask for user repair guidance after the node test result is available.
This coordinator does not execute the generated graph or implement a ReAct judge.
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

from ag_ui_workflow import StepRunOutput, WorkflowStepNode

from flowx_core.architect.graph import NodeMeta
from flowx_core.tools.artifact_tools import (
	fingerprint_json,
	json_text,
	make_test_relocatable,
	node_source_artifact,
	render_json_context,
	write_json_atomic,
)
from flowx_core.tools.workflow_engine import (
	FlowXWorkflowEngine,
	close_workflow_engine,
	init_workflow_engine,
	reload_workflow_engine,
)
from flowx_core.tools.workflow_logs import WorkflowNodeLogs
from flowx_core.workflows.node_generation import NodeGenerationRequest, NodeGenerationWorkflow
from flowx_core.workflows.node_runtime_modifier import (
	NodeRuntimeModifierPipeline,
	NodeRuntimeModifierRequest,
)


@dataclass(frozen=True)
class NodeRepairInput:
	"""The fresh node-test result offered to the caller for amendment guidance."""

	node_name: str
	attempt: int
	test_log: str
	test_result: Mapping[str, Any] | None


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


def _audit_report(violations: list[Any]) -> str:
	return "\n".join(f"Line {item.lineno}: {item.rule} - {item.detail}" for item in violations)


@dataclass(frozen=True, kw_only=True)
class _GenerationRun:
	"""Inputs attached to one native run, never retained by pooled step instances."""

	request: DynamicWorkflowRequest
	directory: Path
	requirement_text: str
	raw_nodes: Mapping[str, Mapping[str, Any]]
	artifacts: Mapping[str, Mapping[str, Path]]
	registry: Mapping[str, Any]


class _NodeGenerationStep(WorkflowStepNode):
	"""One concrete subclass per JSON node; the request selects node generation, repair or both."""

	INPUT_REQUIRED = False

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input
		try:
			if any(output.derived.get("error") for output in dependency_results.values()):
				return StepRunOutput(
					card={"status": "blocked", "node_name": self.STEP_ID},
					derived={"error": f"upstream generation failed for {self.STEP_ID}"},
				)
			run = session_state.get("dynamic_workflow_run")
			if not isinstance(run, _GenerationRun):
				raise TypeError("dynamic workflow generation run is missing")
			logs = session_state.get("dynamic_workflow_logs")
			if not isinstance(logs, WorkflowNodeLogs):
				raise TypeError("dynamic workflow node logs are missing")
			if set(dependency_results) != set(self.DEPENDENCIES):
				raise RuntimeError(f"generation dependencies are incomplete for {self.STEP_ID}")
			# Each parent's output already carries all of its ancestors' artifacts.
			upstream = {
				name: artifact
				for output in dependency_results.values()
				for name, artifact in output.derived["artifacts"].items()
			}
			return self._build_node(run, logs, upstream)
		except Exception as exc:
			# Do not trigger native fail-fast cancellation while siblings still write
			# candidate files. Drain the DAG, mark dependents blocked, then raise at
			# the caller boundary before publishing anything or releasing candidates.
			return StepRunOutput(
				card={"status": "failed", "node_name": self.STEP_ID},
				derived={"error": f"{type(exc).__name__}: {exc}"},
			)

	def _build_node(
		self, run: _GenerationRun, logs: WorkflowNodeLogs, upstream: Mapping[str, Mapping[str, Any]],
	) -> StepRunOutput:
		"""Generate and/or repair this node; version metadata is published by the caller."""
		name, request, directory = self.STEP_ID, run.request, run.directory
		generation, _ = _published_workflows()
		raw_node, targets = run.raw_nodes[name], run.artifacts[name]
		audit_result: tuple[bool, list[Any]] | None = None
		dependency_digests = {parent: artifact["source_digest"] for parent, artifact in upstream.items()}
		context = render_json_context(
			upstream,
			heading="Upstream generation artifacts (static, not runtime outputs):",
		)
		# Amendment mode does not consume node_meta itself. Keep graph edits visible
		# to the coder as well as to its downstream auditor.
		context += "\nCurrent target-node specification (authoritative):\n" + json_text(raw_node)
		if request.markdown_amendments.get(name):
			context += "\nApply these node-brief changes to the implementation too:\n" + request.markdown_amendments[name]
		fingerprint = fingerprint_json({
			"node": raw_node, "requirement": run.requirement_text,
			"dependencies": dependency_digests,
			"node_format": dict(request.node_formats.get(name, {})),
			"generation": {
				"temperature": request.temperature, "generate_markdown": request.generate_markdown,
				"skip_node_planner": request.skip_node_planner, "modifier": request.run_runtime_modifier,
				"test_temperature": request.test_temperature, "amendment_temperature": request.amendment_temperature,
				"python_command": request.python_command, "timeout": request.timeout,
			},
		})
		registry = run.registry.get(name)
		prior = registry["versions"][-1] if registry else None
		dirty = (
			prior is None or prior["input_fingerprint"] != fingerprint or name in request.force_nodes
			or bool(request.node_amendments.get(name)) or bool(request.markdown_amendments.get(name))
		)
		if dirty:
			for path in targets.values():
				path.parent.mkdir(parents=True, exist_ok=True)
			# An old passing log/test is not evidence about new code.
			for key in ("test", "log", "repair_log"):
				targets[key].unlink(missing_ok=True)
			if request.run_node_generation:
				if request.generate_markdown and not request.skip_node_planner and not request.markdown_amendments.get(name):
					targets["markdown"].unlink(missing_ok=True)
				amendment = request.node_amendments.get(name, "")
				if prior and not amendment:
					amendment = "Update this node to match the current workflow specification, requirement and upstream context."
				generated = generation.run(NodeGenerationRequest(
					graph_plan_path=str(directory / "workflow.json"),
					requirement_md_path=str(directory / "requirement_analysis.md"), requirement_text=run.requirement_text,
					node_meta=NodeMeta.from_dict(raw_node), raw_node=raw_node,
					target_path=targets["source"], markdown_path=targets["markdown"],
					temperature=request.temperature, generate_markdown=request.generate_markdown,
					skip_node_planner=request.skip_node_planner,
					markdown_amendment=request.markdown_amendments.get(name, ""), node_amendment=amendment,
					node_format=request.node_formats.get(name, {}), dependency_context=context, state=dict(request.state),
				))
				if Path(generated).resolve() != targets["source"] or not targets["source"].is_file():
					raise ValueError("node generation must produce the requested candidate source")
			elif not targets["source"].is_file():
				raise FileNotFoundError(f"node {name} has no saved source to repair")
			if request.run_runtime_modifier:
				feedback = ""
				for attempt in range(request.max_repair_retries + 1):
					try:
						audit_result = self._repair(run, logs, attempt, feedback)
					except Exception:
						if attempt == request.max_repair_retries:
							raise
						# A failing test is amended from the fresh log of the next attempt's own run.
						feedback = ""
						continue
					ok, violations = audit_result
					if ok:
						break
					feedback = _audit_report(violations) or "node audit failed"
			if targets["test"].is_file():
				make_test_relocatable(targets["test"], directory)
		if audit_result is None:
			audit_result = generation.node_auditor.audit_node_file(
				str(targets["source"]), node_meta=NodeMeta.from_dict(raw_node),
				graph_plan_path=str(directory / "workflow.json"),
			)
		ok, violations = audit_result
		logs.record_audit(name, _audit_report(violations) or "passed")
		if not ok:
			raise RuntimeError(f"node audit failed for {name}: {violations}")
		return StepRunOutput(
			card={"status": "generated" if dirty else "reused", "node_name": name},
			derived={
				"node_id": registry["node_id"] if registry else uuid.uuid4().hex,
				"version": (registry["version"] if registry else 0) + int(bool(dirty)),
				"input_fingerprint": fingerprint, "changed": bool(dirty),
				"artifacts": {**upstream, name: node_source_artifact(name, targets["source"])},
			},
		)

	def _ask_repair_input(
		self,
		run: _GenerationRun,
		request: NodeRuntimeModifierRequest,
		attempt: int,
	) -> str:
		"""Offer the fresh test result to the request's repair callback."""
		callback = run.request.repair_input_callback
		if callback is None:
			return ""
		value = callback(NodeRepairInput(
			node_name=self.STEP_ID,
			attempt=attempt,
			test_log=request.test_log,
			test_result=request.test_result,
		))
		return "" if value is None else str(value)

	def _repair(
		self, run: _GenerationRun, logs: WorkflowNodeLogs, attempt: int, feedback: str = "",
	) -> tuple[bool, list[Any]]:
		"""Test, ask for guidance after a failure, amend, then retest and re-audit.

		``feedback`` (audit violations from the previous attempt) is applied as an amendment first.
		"""
		name, request, directory = self.STEP_ID, run.request, run.directory
		generation, modifier = _published_workflows()
		targets = run.artifacts[name]
		modifier_request = NodeRuntimeModifierRequest(
			node_file_name=str(targets["source"]), node_name=name,
			workflow_graph=str(directory / "workflow.json"), working_directory=str(directory),
			test_file_name=str(targets["test"]), log_file_name=str(targets["log"]),
			python_command=request.python_command, timeout=request.timeout,
			test_temperature=request.test_temperature, amendment_temperature=request.amendment_temperature,
			# Only the first attempt writes the test; retries reuse it.
			skip_generate_node_test=targets["test"].is_file(),
			state=dict(request.state),
		)
		if feedback:
			# The violations are in no test log: amend from them as if they were one, then retest.
			targets["log"].parent.mkdir(parents=True, exist_ok=True)
			targets["log"].write_text(feedback, encoding="utf-8")
			modifier_request.skip_run_node_test = True
			amended = modifier.run(modifier_request)
			if Path(amended.amended_node_file_path).resolve() != targets["source"]:
				raise ValueError("modifier must keep the candidate node source path")
			modifier_request.skip_run_node_test = False

		interactive = getattr(modifier, "run_interactive", None)
		if interactive is None:
			result = modifier.run(modifier_request)
		else:
			# The callback is invoked at the amend gate, after run_node_test.
			result = interactive(
				modifier_request,
				lambda current: self._ask_repair_input(run, current, attempt),
			)
		logs.record_test(name, result.test_log)
		if Path(result.amended_node_file_path).resolve() != targets["source"]:
			raise ValueError("modifier must keep the candidate node source path")
		if result.test_result is None:
			raise RuntimeError(f"node modifier produced no test result for {name}")
		if not result.test_result["ok"]:
			if targets["log"].is_file():
				shutil.copy2(targets["log"], targets["repair_log"])
			# A repair's original failure is not proof that the amended code passes.
			modifier_request.skip_generate_node_test = True
			modifier_request.skip_amend_node = True
			verified = modifier.run(modifier_request)
			logs.record_test(name, verified.test_log)
			if verified.test_result is None or not verified.test_result["ok"]:
				raise RuntimeError(f"node tests still fail after repair for {name}: {verified.test_log}")
		ok, violations = generation.node_auditor.audit_node_file(
			str(targets["source"]), node_meta=NodeMeta.from_dict(run.raw_nodes[name]),
			graph_plan_path=str(directory / "workflow.json"),
		)
		return ok, violations


class DynamicWorkflowOrchestrator:
	"""Version and publish the nodes of one persisted workflow ID.

	``storage_dir`` belongs to one workflow. IDs survive reload, deletion and
	re-addition of the same node name; renaming a node is currently remove+add.
	``keep_last_versions=None`` retains all history; a positive N retains the
	last N workflow revisions and, independently, the last N revisions per node.
	Workflow bundles contain copies, so node-history pruning cannot break them.
	One WorkflowEngine schedules the JSON dependency graph and reloads its pool when
	the topology changes. Its steps use the node generation workflow and runtime
	modifier that AgentBuilder publishes and closes; this class owns neither.

	Runs on this instance are serialized. A second writer encountering an existing
	candidate directory fails rather than overwriting it. This is not a distributed
	revision store.
	"""

	def __init__(
		self,
		*,
		storage_dir: str | Path,
		workflow_id: str | None = None,
		keep_last_versions: int | None = None,
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
		self._lock = RLock()
		self._closed = False
		self._generation_engine: FlowXWorkflowEngine | None = None
		self._generation_config: dict[str, Any] | None = None
		self._last_logs: WorkflowNodeLogs | None = None
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
		with self._lock:
			return self._read_manifest()["workflow_version"]

	@property
	def generated_node_id_map(self) -> dict[str, str]:
		with self._lock:
			workflows = self._read_manifest()["workflows"]
			return {name: node["node_id"] for name, node in workflows[-1]["nodes"].items()} if workflows else {}

	@property
	def generation_engine(self) -> FlowXWorkflowEngine | None:
		"""The one scheduling engine, or None before the first nonempty run."""
		with self._lock:
			return self._generation_engine

	@property
	def last_logs(self) -> WorkflowNodeLogs | None:
		"""Test and audit log text by node step_id from the latest run, including a failed one."""
		with self._lock:
			return self._last_logs

	def _init_generation_engine(self, config: dict[str, Any]) -> FlowXWorkflowEngine | None:
		"""Create the engine once, then reload its pool when the topology or pool size changes."""
		if not config["nodes"]:
			return None  # Nothing to schedule; the engine keeps its last topology.
		if config == self._generation_config:
			return self._generation_engine
		# Native construction receives real zero-argument classes, not factories,
		# closures holding requests, generated business-code imports or instances.
		node_classes = {
			node["name"]: type(f"{node['name']}GenerationStep", (_NodeGenerationStep,), {
				"__module__": __name__,
				"STEP_ID": node["name"],
				"TITLE": f"Generate {node['name']}",
				"DEPENDENCIES": list(node["depends"]),
			})
			for node in config["nodes"]
		}
		if self._generation_engine is None:
			self._generation_engine = init_workflow_engine(
				node_classes, config, graph_config_id=f"{self.workflow_id}:generation",
			)
		else:
			reload_workflow_engine(self._generation_engine, node_classes, config)
		self._generation_config = config
		return self._generation_engine

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
		with self._lock:
			return tuple(self._workflow_version(item) for item in self._read_manifest()["workflows"])

	def get_workflow_version(self, version: int | None = None) -> WorkflowVersion:
		versions = self.list_workflow_versions()
		for item in reversed(versions):
			if version is None or item.version == version:
				return item
		raise KeyError(f"workflow version not retained: {version}")

	def list_node_versions(self, node_id: str) -> tuple[NodeVersion, ...]:
		with self._lock:
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

	def run(self, request: DynamicWorkflowRequest) -> WorkflowVersion:
		"""Publish a complete revision, or return the existing revision for a no-op.

		Source is included in child generation context, so a changed parent source
		invalidates its children. No requests, native nodes or state dictionaries
		are retained. Failed candidates do not change IDs, counters or current head.
		"""
		with self._lock:
			if self._closed:
				raise RuntimeError("dynamic workflow orchestrator is closed")
			_published_workflows()
			if not (request.run_node_generation or request.run_runtime_modifier):
				raise ValueError("enable node generation, the runtime modifier, or both")
			if not request.run_node_generation and (request.node_amendments or request.markdown_amendments):
				raise ValueError("node and markdown amendments require node generation")
			graph, order = self._load_graph(request.workflow_json_path)
			requirement_text = Path(request.requirement_md_path).expanduser().resolve().read_text(encoding="utf-8")
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
			engine = self._init_generation_engine(generation_config)
			manifest = self._read_manifest()
			version = manifest["workflow_version"] + 1
			directory = self.storage_dir / "workflows" / f"{version:08d}"
			# Never overwrite another writer's candidate or an interrupted build.
			directory.mkdir(parents=True, exist_ok=False)
			created_node_dirs: list[Path] = []
			logs = self._last_logs = WorkflowNodeLogs()
			committed = False
			try:
				self._write_json(directory / "workflow.json", graph)
				self._write_json(directory / "generation_workflow.json", generation_config)
				(directory / "requirement_analysis.md").write_text(requirement_text, encoding="utf-8")
				raw_nodes = {node["name"]: node for node in graph["nodes"]}
				artifacts: dict[str, dict[str, Path]] = {}
				node_records, changed = {}, []
				# Make all prior sources available for graph-aware ancestor inspection.
				for name in order:
					registry = manifest["nodes"].get(name)
					targets = self._artifact_targets(directory, name)
					artifacts[name] = targets
					if registry:
						self._copy_artifacts(self._node_version(registry["versions"][-1]).artifacts, targets)
				# Only the engine executes generation/repair. The loops outside it
				# prepare immutable inputs and publish outputs after all workers stop.
				if engine is not None:
					result = engine.run_steps(state={"dynamic_workflow_run": _GenerationRun(
						request=request, directory=directory,
						requirement_text=requirement_text, raw_nodes=raw_nodes,
						artifacts=artifacts, registry=manifest["nodes"],
					), "dynamic_workflow_logs": logs})
					failed = {
						name: result.outputs[name].derived["error"]
						for name in order if result.outputs[name].derived.get("error")
					}
					if failed:
						root_causes = {
							name: error for name, error in failed.items()
							if result.outputs[name].card.get("status") == "failed"
						}
						raise DynamicWorkflowError(
							"dynamic workflow generation failed:\n"
							+ "\n".join(f"{name}: {error}" for name, error in failed.items()),
							root_causes,
						)
					for name in order:
						output = result.outputs[name].derived
						node_record = self._record_node(
							output["node_id"], name, output["version"], output["input_fingerprint"], artifacts[name],
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
				graph_fingerprint = fingerprint_json({"graph": graph, "requirement": requirement_text})
				if not changed and manifest["workflows"] and manifest["workflows"][-1]["input_fingerprint"] == graph_fingerprint:
					return self._workflow_version(manifest["workflows"][-1])
				workflow_record = {
					"workflow_id": self.workflow_id,
					"version": version, "input_fingerprint": graph_fingerprint,
					"workflow_path": str((directory / "workflow.json").relative_to(self.storage_dir)),
					"generation_workflow_path": str((directory / "generation_workflow.json").relative_to(self.storage_dir)),
					"requirement_md_path": str((directory / "requirement_analysis.md").relative_to(self.storage_dir)),
					"nodes": node_records, "changed_nodes": changed,
				}
				self._write_json(directory / "version.json", workflow_record)
				manifest["workflow_version"] = version
				manifest["workflows"].append(workflow_record)
				removed = self._trim_history(manifest)
				self._write_json(self._manifest_path, manifest)
				committed = True
				for path in removed:
					shutil.rmtree(path, ignore_errors=True)
				return self._workflow_version(workflow_record)
			finally:
				if not committed:
					shutil.rmtree(directory, ignore_errors=True)
					for node_dir in created_node_dirs:
						shutil.rmtree(node_dir, ignore_errors=True)

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
		with self._lock:
			if self._closed:
				raise RuntimeError("dynamic workflow orchestrator is closed")
			manifest = self._read_manifest()
			removed = self._trim_history(manifest)
			self._write_json(self._manifest_path, manifest)
			for path in removed:
				shutil.rmtree(path, ignore_errors=True)

	def close(self) -> None:
		"""Close the scheduling engine; AgentBuilder closes the published child workflows."""
		with self._lock:
			if self._closed:
				return
			self._closed = True
			engine, self._generation_engine = self._generation_engine, None
			self._generation_config = None
			if engine is not None:
				close_workflow_engine(engine)


__all__ = [
	"DynamicWorkflowError", "DynamicWorkflowOrchestrator", "DynamicWorkflowRequest",
	"NodeRepairInput", "NodeVersion", "WorkflowVersion",
]
