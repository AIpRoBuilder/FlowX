# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""Expanded business-node graph with per-node request namespaces.

Every business node is expanded into its generation, guard, repair-loop and
finalization steps inside one native business engine.  The child generation and
runtime-modifier workflows are therefore graph definitions, not nested engine
calls.  Each expanded step reads only its business node's namespace so concurrent
branches cannot overwrite another node's request state.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, Callable, Mapping

from ag_ui_workflow import (
	GraphSpec,
	LoopPolicy,
	StepRunOutput,
	WorkflowStepNode,
	merge_graphs,
	rebind_step_class,
)

from flowx_core.architect.graph import NodeMeta
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools._node_settings import node_settings
from flowx_core.tools.artifact_tools import (
	fingerprint_json,
	json_text,
	make_test_relocatable,
	node_source_artifact,
	render_json_context,
)
from flowx_core.tools.node_state import NAMESPACE_KEY, get_namespace
from flowx_core.tools.workflow_engine import FlowXWorkflowEngine
from flowx_core.tools.workflow_logs import WorkflowNodeLogs
from flowx_core.workflows.node_generation import (
	NodeGenerationRequest,
	NodeGenerationWorkflow,
	build_node_request,
	build_plan_request,
)
from flowx_core.workflows.node_runtime_modifier import (
	ExecuteNodeTest,
	NodeRuntimeModifierPipeline,
	NodeRuntimeModifierRequest,
	prepare_request,
)

REPAIR_STAGES = frozenset({
	"generate_node_test", "run_node_test", "amend_node", "verify_node_test",
	"audit_amended_node", "amend_from_audit",
})
GENERATION_STAGES = frozenset({"node_plan", "node_generate"})
EXPANDED_STEP_SUFFIXES = (
	"enter", "node_plan", "node_generate", "node_audit", "repair_audit_guard",
	"generate_node_test", "run_node_test", "amend_node", "verify_node_test",
	"audit_amended_node", "amend_from_audit", "finalize", "repair_loop",
)


@dataclass(frozen=True)
class NodeRepairInput:
	"""The fresh node-test result offered to the caller for amendment guidance."""

	node_name: str
	attempt: int
	test_log: str
	test_result: Mapping[str, Any] | None


@dataclass
class NodeBuildNamespace:
	"""All request-local data for one business node expansion."""

	node_name: str
	raw_node: Mapping[str, Any]
	node_meta: NodeMeta
	targets: Mapping[str, Path]
	upstream: Mapping[str, Mapping[str, Any]]
	fingerprint: str = ""
	prior: Mapping[str, Any] | None = None
	dirty: bool = False
	skip_build: bool = False
	skip_generation: bool = False
	skip_plan: bool = False
	skip_repair: bool = False
	repair_enabled: bool = True
	audit_amendment_pending: bool = False
	blocked: bool = False
	error: str = ""
	generation_state: dict[str, Any] = field(default_factory=dict)
	repair_state: dict[str, Any] = field(default_factory=dict)
	repair_attempt: int = 0
	repair_callback: Callable[[NodeRepairInput], str] | None = None

	def repair_guidance(self, request: NodeRuntimeModifierRequest) -> str:
		"""Invoke the caller's per-attempt repair guidance callback."""
		if self.repair_callback is None:
			return ""
		self.repair_attempt += 1
		value = self.repair_callback(NodeRepairInput(
			node_name=self.node_name,
			attempt=self.repair_attempt,
			test_log=request.test_log,
			test_result=request.test_result,
		))
		return "" if value is None else str(value)


def _audit_text(violations: list[Any]) -> str:
	if not violations:
		return "passed"
	return "\n".join(
		f"Line {item.get('lineno', 0)}: {item.get('rule', '')} - {item.get('detail', '')}"
		if isinstance(item, Mapping)
		else f"Line {getattr(item, 'lineno', 0)}: {getattr(item, 'rule', '')} - {getattr(item, 'detail', '')}"
		for item in violations
	)


def _skip_stage(namespace: NodeBuildNamespace, stage: str) -> bool:
	if namespace.error:
		return True
	if stage == "node_plan":
		return namespace.skip_build or namespace.skip_generation or namespace.skip_plan
	if stage in GENERATION_STAGES:
		return namespace.skip_build or namespace.skip_generation
	if stage == "node_audit":
		return False
	if stage == "repair_audit_guard":
		return False
	if stage in REPAIR_STAGES:
		return (
			namespace.skip_build
			or namespace.skip_repair
			or not namespace.repair_enabled
		)
	return False


def _scoped_class(
	base: type[Any],
	*,
	node_name: str,
	step_id: str,
	dependencies: list[str],
	stage: str,
	repeatable: bool = False,
) -> type[Any]:
	"""Create a zero-argument class bound to one business node namespace."""
	state_kind = "repair" if stage in REPAIR_STAGES else "generation"

	class Scoped(base):
		NODE_KEY = node_name
		STATE_KIND = state_kind
		STAGE = stage
		STEP_ID = step_id
		DEPENDENCIES = list(dependencies)
		REPEATABLE = repeatable

		def process_input(self, user_input, dependency_results, session_state):
			namespace = get_namespace(session_state, self.NODE_KEY)
			if _skip_stage(namespace, self.STAGE):
				return StepRunOutput(
					card={"status": "skipped", "node_name": self.NODE_KEY},
					derived={"skipped": True},
				)
			state = (
				namespace.repair_state
				if self.STATE_KIND == "repair"
				else namespace.generation_state
			)
			try:
				return base.process_input(self, user_input, dependency_results, state)
			except Exception as exc:
				return StepRunOutput(
					card={"status": "failed", "node_name": self.NODE_KEY},
					derived={"error": f"{type(exc).__name__}: {exc}"},
				)

	# Request builders key the coder action by class name.  Keep the published
	# class name so a scoped coder reads the action entry seeded for it.
	Scoped.__name__ = base.__name__
	Scoped.__qualname__ = Scoped.__name__
	Scoped.__module__ = __name__
	return Scoped


class _ScopedTestStep:
	"""Mixin adding test-log publication to a scoped test step."""

	def process_input(self, user_input, dependency_results, session_state):
		namespace = get_namespace(session_state, self.NODE_KEY)
		request = namespace.repair_state.get("node_runtime_modifier")
		if (
			self.STAGE == "verify_node_test"
			and not _skip_stage(namespace, self.STAGE)
			and isinstance(request, NodeRuntimeModifierRequest)
			and request.test_result is not None
			and request.test_result.get("ok")
		):
			# A passing first test needs no amendment, so its verification is
			# already established by the same result.
			return StepRunOutput(
				card={"status": "skipped", "node_name": self.NODE_KEY},
				derived={
					**dict(request.test_result),
					"log_file_path": request.log_file_path,
					"skipped": True,
				},
			)
		output = super().process_input(user_input, dependency_results, session_state)
		logs = session_state.get("dynamic_workflow_logs")
		if isinstance(request, NodeRuntimeModifierRequest) and isinstance(logs, WorkflowNodeLogs):
			if request.test_log:
				logs.record_test(self.NODE_KEY, request.test_log)
		return output


def _test_scoped_class(
	*,
	node_name: str,
	step_id: str,
	dependencies: list[str],
	stage: str,
) -> type[Any]:
	base = _scoped_class(
		ExecuteNodeTest,
		node_name=node_name,
		step_id=step_id,
		dependencies=dependencies,
		stage=stage,
		repeatable=True,
	)
	return type(f"{node_name}_{stage}", (_ScopedTestStep, base), {
		"__module__": __name__,
		"NODE_KEY": node_name,
		"STATE_KIND": "repair",
		"STAGE": stage,
		"STEP_ID": step_id,
		"DEPENDENCIES": list(dependencies),
		"REPEATABLE": True,
	})


def _amend_scoped_class(
	base_cls: type[Any],
	*,
	node_name: str,
	step_id: str,
	dependencies: list[str],
) -> type[Any]:
	base = _scoped_class(
		base_cls,
		node_name=node_name,
		step_id=step_id,
		dependencies=dependencies,
		stage="amend_node",
		repeatable=True,
	)

	class ScopedAmend(base):
		def wait_function(self) -> bool:
			"""The expanded graph resolves guidance in process_input, not a native pause."""
			param = self.get_workflow_param()
			namespace = get_namespace(param.state, self.NODE_KEY)
			request = namespace.repair_state.get("node_runtime_modifier")
			with param.condition:
				if _skip_stage(namespace, "amend_node"):
					param.set_state(self.STEP_ID, "running")
					return True
				if not isinstance(request, NodeRuntimeModifierRequest):
					param.set_state(self.STEP_ID, "running")
					return True
				if request.skip_amend_node or (
					request.test_result is not None and request.test_result["ok"]
				):
					param.set_state(self.STEP_ID, "running")
					return True
				param.set_state(self.STEP_ID, "running")
				return True

		def process_input(self, user_input, dependency_results, session_state):
			namespace = get_namespace(session_state, self.NODE_KEY)
			if _skip_stage(namespace, "amend_node"):
				return StepRunOutput(
					card={"status": "skipped", "node_name": self.NODE_KEY},
					derived={"skipped": True},
				)
			request = namespace.repair_state.get("node_runtime_modifier")
			if (
				isinstance(request, NodeRuntimeModifierRequest)
				and not user_input
				and request.test_result is not None
				and not request.test_result["ok"]
				and not request.skip_amend_node
			):
				guidance = namespace.repair_guidance(request)
				if guidance:
					user_input = guidance
			return super().process_input(
				user_input,
				dependency_results,
				session_state,
			)

	ScopedAmend.__name__ = f"{node_name}_amend_node"
	ScopedAmend.__qualname__ = ScopedAmend.__name__
	ScopedAmend.__module__ = __name__
	return ScopedAmend


def _audit_feedback(violations: list[Any]) -> str:
	return "\n".join(
		f"Line {item.get('lineno', 0)}: {item.get('rule', '')} - {item.get('detail', '')}"
		if isinstance(item, Mapping)
		else f"Line {getattr(item, 'lineno', 0)}: {getattr(item, 'rule', '')} - {getattr(item, 'detail', '')}"
		for item in violations
	) or "node audit failed"


class _AmendFromAudit:
	"""Mixin implementing an amendment driven by the latest audit result."""

	def wait_function(self) -> bool:
		return True

	def process_input(self, user_input, dependency_results, session_state):
		del user_input
		namespace = get_namespace(session_state, self.NODE_KEY)
		if _skip_stage(namespace, "amend_from_audit"):
			return StepRunOutput(
				card={"status": "skipped", "node_name": self.NODE_KEY},
				derived={"skipped": True},
			)
		audit = dependency_results.get(f"{self.NODE_KEY}__audit_amended_node")
		request = namespace.repair_state.get("node_runtime_modifier")
		if not isinstance(request, NodeRuntimeModifierRequest):
			return StepRunOutput(
				card={"status": "failed", "node_name": self.NODE_KEY},
				derived={"error": "node runtime modifier request is missing"},
			)
		workdir = Path(request.working_directory).expanduser().resolve()
		node_path = Path(request.node_file_name)
		if not node_path.is_absolute():
			node_path = workdir / node_path
		if audit is None or audit.derived.get("ok"):
			request.amended_node_file_path = str(node_path.resolve())
			return StepRunOutput(
				card={"kind": "node-audit-amendment", "status": "skipped"},
				derived={"amended_node_file_path": request.amended_node_file_path},
			)
		graph_path = Path(request.workflow_graph)
		if not graph_path.is_absolute():
			graph_path = workdir / graph_path
		try:
			amended = self.amend_code_with_feedback(
				str(node_path.resolve()),
				_audit_feedback(list(audit.derived.get("violations", []))),
				graph_plan_path=str(graph_path),
				current_node_name=self.NODE_KEY,
				temperature=request.amendment_temperature,
			)
		except Exception as exc:
			return StepRunOutput(
				card={"status": "failed", "node_name": self.NODE_KEY},
				derived={"error": f"{type(exc).__name__}: {exc}"},
			)
		request.amended_node_file_path = str(Path(amended).resolve())
		namespace.audit_amendment_pending = True
		return StepRunOutput(
			card={"kind": "node-audit-amendment", "status": "completed"},
			derived={"amended_node_file_path": request.amended_node_file_path},
		)


def _amend_from_audit_class(
	base_cls: type[Any],
	*,
	node_name: str,
	step_id: str,
	dependencies: list[str],
) -> type[Any]:
	scoped = _scoped_class(
		base_cls,
		node_name=node_name,
		step_id=step_id,
		dependencies=dependencies,
		stage="amend_from_audit",
		repeatable=True,
	)

	class ScopedAuditAmend(_AmendFromAudit, scoped):
		pass

	ScopedAuditAmend.__name__ = f"{node_name}_amend_from_audit"
	ScopedAuditAmend.__qualname__ = ScopedAuditAmend.__name__
	ScopedAuditAmend.__module__ = __name__
	return ScopedAuditAmend


class _RepairAuditGuard(WorkflowStepNode):
	"""Stop the repair branch when generation's audit did not pass."""

	INPUT_REQUIRED = False
	NODE_KEY = ""

	def process_input(self, user_input, dependency_results, session_state):
		del user_input
		namespace = get_namespace(session_state, self.NODE_KEY)
		if namespace.error:
			return StepRunOutput(
				card={"status": "blocked", "node_name": self.NODE_KEY},
				derived={"error": namespace.error},
			)
		audit = dependency_results.get(f"{self.NODE_KEY}__node_audit")
		ok = bool(audit and audit.derived.get("ok"))
		namespace.repair_enabled = ok
		request = namespace.repair_state.get("node_runtime_modifier")
		if not ok and isinstance(request, NodeRuntimeModifierRequest):
			request.skip_generate_node_test = True
			request.skip_run_node_test = True
			request.skip_amend_node = True
		return StepRunOutput(
			card={"kind": "repair-audit-guard", "status": "passed" if ok else "skipped"},
			derived={"audit_ok": ok, "repair_enabled": ok},
		)


class _AuditAmendedNode(NodeAuditor):
	"""Audit the amended source inside the repair loop."""

	INPUT_REQUIRED = False
	REPEATABLE = True
	NODE_KEY = ""

	def process_input(self, user_input, dependency_results, session_state):
		del user_input
		namespace = get_namespace(session_state, self.NODE_KEY)
		if _skip_stage(namespace, "audit_amended_node"):
			return StepRunOutput(
				card={"status": "skipped", "node_name": self.NODE_KEY},
				derived={"skipped": True},
			)
		request = namespace.repair_state.get("node_runtime_modifier")
		if not isinstance(request, NodeRuntimeModifierRequest):
			return StepRunOutput(
				card={"status": "failed", "node_name": self.NODE_KEY},
				derived={"error": "node runtime modifier request is missing"},
			)
		workdir = Path(request.working_directory).expanduser().resolve()
		node_path = Path(request.node_file_name)
		if not node_path.is_absolute():
			node_path = workdir / node_path
		ok, violations = self.audit_file(
			str(node_path.resolve()),
			node_meta=namespace.node_meta,
			graph_plan_path=str(request.workflow_graph),
		)
		derived = {
			"file_path": str(node_path),
			"ok": ok,
			"violations": [asdict(item) for item in violations],
		}
		logs = session_state.get("dynamic_workflow_logs")
		if isinstance(logs, WorkflowNodeLogs):
			logs.record_audit(self.NODE_KEY, _audit_text(derived["violations"]))
		return StepRunOutput(
			card={"kind": "node-audit", "status": "passed" if ok else "failed"},
			derived=derived,
		)


def _prepare_namespace(run: Any, namespace: NodeBuildNamespace) -> None:
	"""Build all namespaced requests for one business node."""
	name = namespace.node_name
	request = run.request
	directory = run.directory
	raw_node = namespace.raw_node
	targets = namespace.targets
	generation = run.generation
	modifier = run.modifier

	node_meta = NodeMeta.from_dict(raw_node)
	dependency_digests = {
		parent: artifact["source_digest"]
		for parent, artifact in namespace.upstream.items()
	}
	context = render_json_context(
		namespace.upstream,
		heading="Upstream generation artifacts (static, not runtime outputs):",
	)
	context += "\nCurrent target-node specification (authoritative):\n" + json_text(raw_node)
	if request.markdown_amendments.get(name):
		context += (
			"\nApply these node-brief changes to the implementation too:\n"
			+ request.markdown_amendments[name]
		)

	fingerprint = fingerprint_json({
		"node": raw_node,
		"requirement": run.requirement_text,
		"dependencies": dependency_digests,
		"node_format": dict(request.node_formats.get(name, {})),
		"generation": {
			"temperature": request.temperature,
			"generate_markdown": request.generate_markdown,
			"skip_node_planner": request.skip_node_planner,
			"modifier": request.run_runtime_modifier,
			"test_temperature": request.test_temperature,
			"amendment_temperature": request.amendment_temperature,
			"python_command": request.python_command,
			"timeout": request.timeout,
		},
	})
	registry = run.registry.get(name)
	prior = registry["versions"][-1] if registry else None
	dirty = (
		prior is None
		or prior["input_fingerprint"] != fingerprint
		or name in request.force_nodes
		or bool(request.node_amendments.get(name))
		or bool(request.markdown_amendments.get(name))
	)

	namespace.node_meta = node_meta
	namespace.fingerprint = fingerprint
	namespace.prior = prior
	namespace.dirty = dirty
	namespace.skip_build = not dirty
	namespace.skip_generation = not request.run_node_generation
	namespace.skip_repair = not request.run_runtime_modifier or not dirty
	namespace.repair_enabled = dirty and request.run_runtime_modifier
	namespace.repair_callback = request.repair_input_callback

	if dirty:
		for path in targets.values():
			path.parent.mkdir(parents=True, exist_ok=True)
		for key in ("test", "log", "repair_log"):
			targets[key].unlink(missing_ok=True)
		if (
			request.run_node_generation
			and request.generate_markdown
			and not request.skip_node_planner
			and not request.markdown_amendments.get(name)
		):
			targets["markdown"].unlink(missing_ok=True)

	if not request.run_node_generation and not targets["source"].is_file():
		raise FileNotFoundError(f"node {name} has no saved source to repair")

	amendment = request.node_amendments.get(name, "")
	if dirty and prior and not amendment:
		amendment = (
			"Update this node to match the current workflow specification, "
			"requirement and upstream context."
		)
	generation_request = NodeGenerationRequest(
		graph_plan_path=str(directory / "workflow.json"),
		requirement_md_path=str(directory / "requirement_analysis.md"),
		requirement_text=run.requirement_text,
		node_meta=node_meta,
		raw_node=raw_node,
		target_path=targets["source"],
		markdown_path=targets["markdown"],
		temperature=request.temperature,
		generate_markdown=request.generate_markdown,
		skip_node_planner=request.skip_node_planner,
		markdown_amendment=request.markdown_amendments.get(name, ""),
		node_amendment=amendment,
		node_format=request.node_formats.get(name, {}),
		dependency_context=context,
		state=dict(request.state),
	)
	plan_request = build_plan_request(generation_request)
	needs_plan = not request.skip_node_planner and (
		bool(plan_request["generate_markdown"]) or bool(plan_request["amendment"])
	)
	namespace.skip_plan = not needs_plan
	namespace.generation_state = {
		**generation_request.state,
		**build_node_request(
			generation_request,
			type(generation.node_coder).__name__,
			plan_request,
		),
	}

	test_exists = targets["test"].is_file()
	skip_repair = namespace.skip_repair
	modifier_request = NodeRuntimeModifierRequest(
		node_file_name=str(targets["source"]),
		node_name=name,
		workflow_graph=str(directory / "workflow.json"),
		working_directory=str(directory),
		test_file_name=str(targets["test"]),
		log_file_name=str(targets["log"]),
		python_command=request.python_command,
		timeout=request.timeout,
		test_temperature=request.test_temperature,
		amendment_temperature=request.amendment_temperature,
		skip_generate_node_test=skip_repair or test_exists,
		skip_run_node_test=skip_repair,
		skip_amend_node=skip_repair,
		state=dict(request.state),
	)
	modifier_request = prepare_request(modifier_request)
	namespace.repair_state = {
		**modifier_request.state,
		"node_runtime_modifier": modifier_request,
	}


class _NodeBuildEntryStep(WorkflowStepNode):
	"""Prepare one business node's namespace before its expanded substeps."""

	INPUT_REQUIRED = False
	NODE_KEY = ""

	def process_input(self, user_input, dependency_results, session_state):
		del user_input
		run = session_state.get("dynamic_workflow_run")
		raw_node = (
			getattr(run, "raw_nodes", {}).get(self.NODE_KEY, {})
			if run is not None
			else {}
		)
		targets = (
			getattr(run, "artifacts", {}).get(self.NODE_KEY, {})
			if run is not None
			else {}
		)
		upstream: dict[str, Mapping[str, Any]] = {}
		for output in dependency_results.values():
			for name, artifact in output.derived.get("artifacts", {}).items():
				upstream[name] = artifact
		namespace = NodeBuildNamespace(
			node_name=self.NODE_KEY,
			raw_node=raw_node,
			node_meta=(
				NodeMeta.from_dict(raw_node)
				if raw_node
				else NodeMeta(name=self.NODE_KEY, type="", desc="")
			),
			targets=targets,
			upstream=upstream,
		)
		try:
			if run is None:
				raise RuntimeError("dynamic workflow generation run is missing")
			if any(output.derived.get("error") for output in dependency_results.values()):
				namespace.blocked = True
				raise RuntimeError(f"upstream generation failed for {self.NODE_KEY}")
			if set(dependency_results) != set(self.DEPENDENCIES):
				raise RuntimeError(f"generation dependencies are incomplete for {self.NODE_KEY}")
			_prepare_namespace(run, namespace)
		except Exception as exc:
			namespace.error = f"{type(exc).__name__}: {exc}"
			namespace.skip_build = True
			namespace.skip_generation = True
			namespace.skip_repair = True
			namespace.repair_enabled = False

		namespaces = session_state.setdefault(NAMESPACE_KEY, {})
		lock = session_state.get("dynamic_workflow_namespace_lock")
		if lock is not None:
			with lock:
				namespaces[self.NODE_KEY] = namespace
		else:
			namespaces[self.NODE_KEY] = namespace

		if namespace.error:
			return StepRunOutput(
				card={
					"status": "blocked" if namespace.blocked else "failed",
					"node_name": self.NODE_KEY,
				},
				derived={"error": namespace.error},
			)
		return StepRunOutput(
			card={"status": "prepared", "node_name": self.NODE_KEY},
			derived={"prepared": True},
		)


class _NodeBuildFinalizeStep(WorkflowStepNode):
	"""Publish one business node's final version, artifacts and status."""

	INPUT_REQUIRED = False
	NODE_KEY = ""

	def process_input(self, user_input, dependency_results, session_state):
		del user_input, dependency_results
		try:
			return self._finalize(session_state)
		except Exception as exc:
			return self._failed(f"{type(exc).__name__}: {exc}")

	def _finalize(self, session_state):
		run = session_state.get("dynamic_workflow_run")
		namespace = get_namespace(session_state, self.NODE_KEY)
		logs = session_state.get("dynamic_workflow_logs")
		if namespace.error:
			return self._failed(namespace.error, blocked=namespace.blocked)

		outputs = self.get_workflow_param().outputs
		chain_ids = (
			f"{self.NODE_KEY}__node_plan",
			f"{self.NODE_KEY}__node_generate",
			f"{self.NODE_KEY}__node_audit",
			f"{self.NODE_KEY}__repair_audit_guard",
			f"{self.NODE_KEY}__generate_node_test",
			f"{self.NODE_KEY}__run_node_test",
			f"{self.NODE_KEY}__amend_node",
			f"{self.NODE_KEY}__verify_node_test",
			f"{self.NODE_KEY}__audit_amended_node",
			f"{self.NODE_KEY}__amend_from_audit",
		)
		for step_id in chain_ids:
			output = outputs.get(step_id)
			if output is not None and output.derived.get("error"):
				return self._failed(output.derived["error"])

		generation_audit = outputs.get(f"{self.NODE_KEY}__node_audit")
		repair_audit = outputs.get(f"{self.NODE_KEY}__audit_amended_node")
		audit_output = (
			repair_audit
			if namespace.repair_enabled
			and not namespace.skip_repair
			and repair_audit is not None
			and not repair_audit.derived.get("skipped")
			else generation_audit
		)
		audit_ok = bool(audit_output and audit_output.derived.get("ok"))
		audit_violations = (
			audit_output.derived.get("violations", []) if audit_output is not None else []
		)
		if isinstance(logs, WorkflowNodeLogs):
			logs.record_audit(self.NODE_KEY, _audit_text(audit_violations))
		if not audit_ok:
			detail = _audit_text(audit_violations) if audit_violations else "audit output missing"
			return self._failed(f"node audit failed for {self.NODE_KEY}: {detail}")
		if namespace.repair_enabled and not namespace.skip_repair:
			verify = outputs.get(f"{self.NODE_KEY}__verify_node_test")
			if verify is None or not verify.derived.get("ok"):
				return self._failed(f"node tests failed for {self.NODE_KEY}")

		test_path = namespace.targets.get("test")
		if test_path is not None and test_path.is_file() and run is not None:
			make_test_relocatable(test_path, run.directory)

		artifact = node_source_artifact(self.NODE_KEY, namespace.targets["source"])
		artifacts = {**namespace.upstream, self.NODE_KEY: artifact}
		if namespace.skip_build:
			prior = namespace.prior or {}
			return StepRunOutput(
				card={"status": "reused", "node_name": self.NODE_KEY},
				derived={
					"node_id": prior.get("node_id") or uuid.uuid4().hex,
					"version": int(prior.get("version", 0)),
					"input_fingerprint": namespace.fingerprint,
					"changed": False,
					"artifacts": artifacts,
				},
			)
		prior = namespace.prior or {}
		return StepRunOutput(
			card={"status": "generated", "node_name": self.NODE_KEY},
			derived={
				"node_id": prior.get("node_id") or uuid.uuid4().hex,
				"version": int(prior.get("version", 0)) + 1,
				"input_fingerprint": namespace.fingerprint,
				"changed": True,
				"artifacts": artifacts,
			},
		)

	def _failed(self, error: str, *, blocked: bool = False) -> StepRunOutput:
		return StepRunOutput(
			card={
				"status": "blocked" if blocked else "failed",
				"node_name": self.NODE_KEY,
			},
			derived={"error": error},
		)


def _entry_class(
	node_name: str,
	generation: NodeGenerationWorkflow,
	modifier: NodeRuntimeModifierPipeline,
) -> type[WorkflowStepNode]:
	del generation, modifier

	class Entry(_NodeBuildEntryStep):
		NODE_KEY = node_name
		STEP_ID = f"{node_name}__enter"
		TITLE = f"Prepare {node_name}"
		DEPENDENCIES: list[str] = []

	Entry.__name__ = f"{node_name}_enter"
	Entry.__module__ = __name__
	return Entry


def _finalize_class(node_name: str) -> type[WorkflowStepNode]:
	class Finalize(_NodeBuildFinalizeStep):
		NODE_KEY = node_name
		STEP_ID = f"{node_name}__finalize"
		TITLE = f"Finalize {node_name}"
		DEPENDENCIES: list[str] = []

	Finalize.__name__ = f"{node_name}_finalize"
	Finalize.__module__ = __name__
	return Finalize


def _guard_class(node_name: str) -> type[WorkflowStepNode]:
	class Guard(_RepairAuditGuard):
		NODE_KEY = node_name
		STEP_ID = f"{node_name}__repair_audit_guard"
		TITLE = f"Guard repair for {node_name}"
		DEPENDENCIES = [f"{node_name}__node_audit"]

	Guard.__name__ = f"{node_name}_repair_audit_guard"
	Guard.__module__ = __name__
	return Guard


def _audit_amended_class(node_name: str) -> type[NodeAuditor]:
	class AuditAmended(_AuditAmendedNode):
		NODE_KEY = node_name
		STEP_ID = f"{node_name}__audit_amended_node"
		TITLE = f"Audit amended {node_name}"
		DEPENDENCIES = [f"{node_name}__verify_node_test"]
		REPEATABLE = True

	AuditAmended.__name__ = f"{node_name}_audit_amended_node"
	AuditAmended.__module__ = __name__
	return AuditAmended


def _repair_policy(node_name: str) -> LoopPolicy:
	def stop(context) -> bool:
		namespace = context.state.get(NAMESPACE_KEY, {}).get(node_name)
		if namespace is None:
			return True
		if namespace.skip_build or namespace.skip_repair or not namespace.repair_enabled:
			return True
		for step in (
			"run_node_test", "amend_node", "verify_node_test",
			"audit_amended_node", "amend_from_audit",
		):
			output = context.output(f"{node_name}__{step}")
			if output is not None and output.derived.get("error"):
				return True
		run = context.state.get("dynamic_workflow_run")
		request = getattr(run, "request", None)
		limit = int(getattr(request, "max_repair_retries", 0)) + 1
		if namespace.audit_amendment_pending:
			limit += 1
		if context.iteration >= limit:
			return True
		verify = context.output(f"{node_name}__verify_node_test")
		audit = context.output(f"{node_name}__audit_amended_node")
		return bool(
			verify is not None
			and verify.derived.get("ok")
			and audit is not None
			and audit.derived.get("ok")
		)

	return LoopPolicy(max_iterations=6, stop_when=stop)


def _settings_for(component: Any) -> dict[str, Any]:
	if not is_dataclass(component):
		return {}
	return {
		setting.name: getattr(component, setting.name)
		for setting in fields(component)
		if setting.init
	}


def _copy_settings(
	settings: dict[str, dict[str, Any]],
	source_id: str,
	target_id: str,
) -> None:
	settings[target_id] = dict(settings.get(source_id, {}))


def _build_expanded_graph(
	*,
	business_nodes: list[dict[str, Any]],
	generation: NodeGenerationWorkflow,
	modifier: NodeRuntimeModifierPipeline,
	finalize_ids: Mapping[str, str] | None = None,
	parent_finalizers: Mapping[str, list[str]] | None = None,
	all_business_names: list[str] | None = None,
	settings_node_names: list[str] | None = None,
	base_graph: GraphSpec | None = None,
) -> tuple[GraphSpec, dict[str, str], dict[str, dict[str, Any]]]:
	"""Build the merged business graph, finalizer ids and native step settings.

	``business_nodes`` may select a subset of the full graph for an incremental
	update.  In that case callers supply the full ``finalize_ids`` and
	``parent_finalizers`` maps, while ``all_business_names`` validates ids across
	the unchanged nodes and ``settings_node_names`` prepares native settings for
	every node that the replacement pool will instantiate.
	"""
	if all_business_names is None:
		_validate_business_nodes(business_nodes)
	node_names = [node["name"] for node in business_nodes]
	all_node_names = list(all_business_names) if all_business_names is not None else node_names
	if len(all_node_names) != len(set(all_node_names)):
		raise ValueError("business workflow node names must be unique")
	if not set(node_names).issubset(all_node_names):
		raise ValueError("selected business nodes must belong to the full business graph")
	expanded_ids = [
		f"{name}__{suffix}" for name in all_node_names for suffix in EXPANDED_STEP_SUFFIXES
	]
	if len(expanded_ids) != len(set(expanded_ids)):
		raise ValueError("business node names produce colliding expanded step ids")

	resolved_finalize_ids = (
		dict(finalize_ids)
		if finalize_ids is not None
		else {name: f"{name}__finalize" for name in all_node_names}
	)
	resolved_parent_finalizers = (
		{name: list(depends) for name, depends in parent_finalizers.items()}
		if parent_finalizers is not None
		else {
			node["name"]: [
				resolved_finalize_ids[parent]
				for parent in node.get("depends", [])
				if parent in resolved_finalize_ids
			]
			for node in business_nodes
		}
	)

	planner_base = type(generation.node_planner)
	coder_base = type(generation.node_coder)
	auditor_base = type(generation.node_auditor)
	test_writer_base = type(modifier.node_test_writer)
	amend_writer_base = type(modifier.node_writer)

	settings: dict[str, dict[str, Any]] = {
		planner_base.STEP_ID: _settings_for(generation.node_planner),
		coder_base.STEP_ID: _settings_for(generation.node_coder),
		auditor_base.STEP_ID: _settings_for(generation.node_auditor),
		ExecuteNodeTest.STEP_ID: {
			"run_subprocess": modifier.run_subprocess,
			"timeout_expired": modifier.timeout_expired,
		},
		test_writer_base.STEP_ID: _settings_for(modifier.node_test_writer),
		amend_writer_base.STEP_ID: _settings_for(modifier.node_writer),
	}
	settings[auditor_base.STEP_ID]["white_list"] = set(
		getattr(generation.node_auditor, "white_list", ())
	)

	def copy_node_settings(name: str) -> None:
		for source_id, target_id in (
			(planner_base.STEP_ID, f"{name}__node_plan"),
			(coder_base.STEP_ID, f"{name}__node_generate"),
			(auditor_base.STEP_ID, f"{name}__node_audit"),
			(test_writer_base.STEP_ID, f"{name}__generate_node_test"),
			(ExecuteNodeTest.STEP_ID, f"{name}__run_node_test"),
			(ExecuteNodeTest.STEP_ID, f"{name}__verify_node_test"),
			(amend_writer_base.STEP_ID, f"{name}__amend_node"),
			(amend_writer_base.STEP_ID, f"{name}__amend_from_audit"),
			(auditor_base.STEP_ID, f"{name}__audit_amended_node"),
		):
			_copy_settings(settings, source_id, target_id)

	for settings_name in (settings_node_names or node_names):
		copy_node_settings(settings_name)

	all_nodes: list[dict[str, Any]] = []
	all_classes: dict[str, Any] = {}
	all_meta: list[dict[str, Any]] = []
	all_hidden: set[str] = set()
	all_loop_bindings: dict[str, Any] = {}

	for node_name in node_names:
		enter_id = f"{node_name}__enter"
		plan_id = f"{node_name}__node_plan"
		generate_id = f"{node_name}__node_generate"
		audit_id = f"{node_name}__node_audit"
		guard_id = f"{node_name}__repair_audit_guard"
		gen_test_id = f"{node_name}__generate_node_test"
		run_test_id = f"{node_name}__run_node_test"
		amend_id = f"{node_name}__amend_node"
		verify_id = f"{node_name}__verify_node_test"
		audit_amended_id = f"{node_name}__audit_amended_node"
		amend_from_audit_id = f"{node_name}__amend_from_audit"
		finalize_id = resolved_finalize_ids[node_name]

		entry_cls = _entry_class(node_name, generation, modifier)
		plan_cls = _scoped_class(
			planner_base, node_name=node_name, step_id=plan_id,
			dependencies=[enter_id], stage="node_plan",
		)
		generate_cls = _scoped_class(
			coder_base, node_name=node_name, step_id=generate_id,
			dependencies=[plan_id], stage="node_generate",
		)
		audit_cls = _scoped_class(
			auditor_base, node_name=node_name, step_id=audit_id,
			dependencies=[generate_id], stage="node_audit",
		)
		guard_cls = _guard_class(node_name)
		gen_test_cls = _scoped_class(
			test_writer_base, node_name=node_name, step_id=gen_test_id,
			dependencies=[guard_id], stage="generate_node_test",
		)
		run_test_cls = _test_scoped_class(
			node_name=node_name, step_id=run_test_id,
			dependencies=[], stage="run_node_test",
		)
		amend_cls = _amend_scoped_class(
			amend_writer_base,
			node_name=node_name, step_id=amend_id,
			dependencies=[run_test_id],
		)
		verify_cls = _test_scoped_class(
			node_name=node_name, step_id=verify_id,
			dependencies=[amend_id], stage="verify_node_test",
		)
		audit_amended_cls = _audit_amended_class(node_name)
		amend_from_audit_cls = _amend_from_audit_class(
			amend_writer_base,
			node_name=node_name,
			step_id=amend_from_audit_id,
			dependencies=[audit_amended_id],
		)
		finalize_cls = _finalize_class(node_name)

		generation_spec = GraphSpec(
			nodes=[
				{"name": enter_id},
				{"name": plan_id, "depends": [enter_id]},
				{"name": generate_id, "depends": [plan_id]},
				{"name": audit_id, "depends": [generate_id]},
				{"name": guard_id, "depends": [audit_id]},
				{"name": gen_test_id, "depends": [guard_id]},
			],
			classes={
				enter_id: entry_cls,
				plan_id: plan_cls,
				generate_id: generate_cls,
				audit_id: audit_cls,
				guard_id: guard_cls,
				gen_test_id: gen_test_cls,
			},
			steps_meta=[
				entry_cls.step_meta(),
				plan_cls.step_meta(),
				generate_cls.step_meta(),
				audit_cls.step_meta(),
				guard_cls.step_meta(),
				gen_test_cls.step_meta(),
			],
		)

		loop_spec_key = "repair-loop"
		loop_spec = GraphSpec(
			nodes=[
				{"name": run_test_id},
				{"name": amend_id, "depends": [run_test_id]},
				{"name": verify_id, "depends": [amend_id]},
				{"name": audit_amended_id, "depends": [verify_id]},
				{"name": amend_from_audit_id, "depends": [audit_amended_id]},
			],
			classes={
				run_test_id: run_test_cls,
				amend_id: amend_cls,
				verify_id: verify_cls,
				audit_amended_id: audit_amended_cls,
				amend_from_audit_id: amend_from_audit_cls,
			},
			steps_meta=[
				run_test_cls.step_meta(),
				amend_cls.step_meta(),
				verify_cls.step_meta(),
				audit_amended_cls.step_meta(),
				amend_from_audit_cls.step_meta(),
			],
			loop_key=loop_spec_key,
			loop_policies={loop_spec_key: _repair_policy(node_name)},
		)
		loop_binding = loop_spec.loop_bindings[loop_spec_key]

		chain = merge_graphs(
			generation_spec,
			loop_spec,
			bridges={run_test_id: [gen_test_id]},
			loop_bindings={"right": {loop_spec_key: loop_binding}},
		)
		wrapper_name = next(
			node["name"] for node in chain.nodes if node.get("meta_type") == "GRegion"
		)
		named_wrapper = f"{node_name}__repair_loop"
		wrapper_node = next(node for node in chain.nodes if node["name"] == wrapper_name)
		wrapper_node["name"] = named_wrapper
		chain.classes[named_wrapper] = chain.classes.pop(wrapper_name)
		if wrapper_name in chain.loop_bindings:
			chain.loop_bindings[named_wrapper] = chain.loop_bindings.pop(wrapper_name)
		finalize_spec = GraphSpec(
			nodes=[{"name": finalize_id}],
			classes={finalize_id: finalize_cls},
			steps_meta=[finalize_cls.step_meta()],
		)
		chain = merge_graphs(
			chain,
			finalize_spec,
			bridges={finalize_id: [named_wrapper]},
		)

		parent_ids = list(resolved_parent_finalizers.get(node_name, []))
		entry_node = next(node for node in chain.nodes if node["name"] == enter_id)
		entry_node["depends"] = parent_ids
		entry_cls = type(entry_cls.__name__, (entry_cls,), {
			"__module__": __name__,
			"STEP_ID": enter_id,
			"DEPENDENCIES": parent_ids,
		})
		chain.classes[enter_id] = entry_cls
		for step in chain.steps_meta:
			if step["id"] == enter_id:
				step["dependencies"] = parent_ids

		all_nodes.extend(chain.nodes)
		all_classes.update(chain.classes)
		all_meta.extend(chain.steps_meta)
		all_hidden.update(chain.hidden_steps)
		all_loop_bindings.update(chain.loop_bindings)

	if base_graph is None:
		spec = GraphSpec(
			nodes=all_nodes,
			classes=all_classes,
			steps_meta=all_meta,
			hidden_steps=all_hidden,
			loop_bindings=all_loop_bindings,
		)
	else:
		loop_policies = dict(base_graph.loop_policies)
		for key, binding in all_loop_bindings.items():
			policy = binding.snapshot()[0]
			if policy is not None:
				loop_policies.setdefault(key, policy)
		spec = GraphSpec(
			nodes=[*base_graph.nodes, *all_nodes],
			classes={**base_graph.classes, **all_classes},
			steps_meta=[*base_graph.steps_meta, *all_meta],
			loop=base_graph.loop,
			loop_key=base_graph.loop_key,
			loop_policies=loop_policies,
			loop_bindings={**base_graph.loop_bindings, **all_loop_bindings},
			hidden_steps=set(base_graph.hidden_steps) | all_hidden,
		)
	return spec, resolved_finalize_ids, settings


def _expanded_chain_ids(node_name: str) -> set[str]:
	return {f"{node_name}__{suffix}" for suffix in EXPANDED_STEP_SUFFIXES}


def _strip_expanded_chains(graph: GraphSpec, node_names: set[str]) -> GraphSpec:
	"""Remove only the exact expanded chains for the named business nodes."""
	if not node_names:
		return graph
	removed = set().union(*(_expanded_chain_ids(name) for name in node_names))
	return GraphSpec(
		nodes=[node for node in graph.nodes if node["name"] not in removed],
		classes={key: value for key, value in graph.classes.items() if key not in removed},
		steps_meta=[step for step in graph.steps_meta if step["id"] not in removed],
		loop=graph.loop,
		loop_key=graph.loop_key,
		loop_policies={
			key: value for key, value in graph.loop_policies.items() if key not in removed
		},
		loop_bindings={
			key: value for key, value in graph.loop_bindings.items() if key not in removed
		},
		hidden_steps={step for step in graph.hidden_steps if step not in removed},
	)


def _leaf_finalizer_ids(
	business_nodes: list[dict[str, Any]],
	finalize_ids: Mapping[str, str],
) -> list[str]:
	"""Finalizer step ids of business nodes that no other enabled node depends on."""
	parents = {parent for node in business_nodes for parent in node.get("depends", [])}
	return [finalize_ids[node["name"]] for node in business_nodes if node["name"] not in parents]


def _with_continuation(
	graph: GraphSpec,
	continuation: GraphSpec | None,
	finalize_ids: Mapping[str, str],
	business_nodes: list[dict[str, Any]],
) -> GraphSpec:
	"""Chain ``continuation`` after every leaf finalizer with bridge dependencies.

	The continuation is a transit join: its root steps wait for all leaf finalizers,
	so whatever follows them runs only once every node chain finished.  The merge
	rewrites the native nodes and step metadata, but a ``WorkflowStepNode`` reads
	``DEPENDENCIES`` from its class, so each bridged root is rebound.
	"""
	if continuation is None or not continuation.nodes:
		return graph
	leaves = _leaf_finalizer_ids(business_nodes, finalize_ids)
	roots = [node["name"] for node in continuation.nodes if not node.get("depends")]
	if not leaves or not roots:
		return graph
	merged = merge_graphs(
		graph,
		continuation,
		bridges={root: leaves for root in roots},
	)
	classes = dict(merged.classes)
	for root in roots:
		classes[root] = rebind_step_class(continuation.classes[root], dependencies=leaves)
	return GraphSpec(
		nodes=merged.nodes,
		classes=classes,
		steps_meta=merged.steps_meta,
		loop=merged.loop,
		loop_key=merged.loop_key,
		loop_policies=merged.loop_policies,
		loop_bindings=merged.loop_bindings,
		hidden_steps=merged.hidden_steps,
	)


def _validate_business_nodes(nodes: list[dict[str, Any]]) -> None:
	"""Check names and dependencies before using a new business DAG."""
	names: set[str] = set()
	for node in nodes:
		name = node.get("name")
		if not isinstance(name, str) or not name:
			raise ValueError("business workflow node names must be non-empty strings")
		if name in names:
			raise ValueError(f"duplicate business workflow node: {name}")
		depends = node.get("depends", [])
		if not isinstance(depends, list) or any(not isinstance(dep, str) for dep in depends):
			raise ValueError(f"node {name} depends must be a list of names")
		if len(set(depends)) != len(depends) or name in depends:
			raise ValueError(f"node {name} has duplicate or self dependencies")
		names.add(name)
	for node in nodes:
		unknown = set(node.get("depends", [])).difference(names)
		if unknown:
			raise ValueError(f"node {node['name']} has undefined dependencies: {sorted(unknown)}")
	TopologicalSorter({
		node["name"]: list(node.get("depends", [])) for node in nodes
	}).static_order()


def _business_descendants(
	nodes: list[dict[str, Any]],
	roots: set[str],
) -> set[str]:
	"""Return dependency descendants of ``roots`` in the supplied business DAG."""
	descendants = set(roots)
	while True:
		added = {
			node["name"] for node in nodes
			if node["name"] not in descendants
			and any(parent in descendants for parent in node.get("depends", []))
		}
		if not added:
			return descendants
		descendants.update(added)


def _business_graph_diff(
	old_nodes: list[dict[str, Any]],
	new_nodes: list[dict[str, Any]],
) -> tuple[set[str], set[str], set[str]]:
	old_map = {node["name"]: node for node in old_nodes}
	new_map = {node["name"]: node for node in new_nodes}
	added = set(new_map).difference(old_map)
	removed = set(old_map).difference(new_map)
	changed = {
		name for name in set(old_map).intersection(new_map)
		if old_map[name] != new_map[name]
	}
	return added, removed, changed


class ExpandedWorkflowEngine(FlowXWorkflowEngine):
	"""One engine whose graph contains the business DAG and every node build chain.

	``update`` replaces the graph in place from a newly supplied business DAG.  The
	underlying pool builds the replacement graph before swapping it in, so active
	leases finish on their old pipelines and a failed build leaves the current
	graph serving.

	``continuation`` is an optional graph chained after the business chains: each of
	its root steps waits for every leaf finalizer, so callers put the stages that
	must run once all node chains finished there (a transit join, then more work).
	A continuation cannot be diffed per business node, so ``update`` rebuilds the
	whole graph whenever the business DAG or the child workflows change.
	"""

	def __init__(
		self,
		*,
		business_nodes: list[dict[str, Any]],
		generation: NodeGenerationWorkflow,
		modifier: NodeRuntimeModifierPipeline,
		pool_size: int = 1,
		graph_config_id: str = "flowx",
		continuation: GraphSpec | None = None,
	) -> None:
		self._init_expanded(
			business_nodes=business_nodes,
			generation=generation,
			modifier=modifier,
			pool_size=pool_size,
			graph_config_id=graph_config_id,
			continuation=continuation,
		)

	def _init_expanded(
		self,
		*,
		business_nodes: list[dict[str, Any]],
		generation: NodeGenerationWorkflow,
		modifier: NodeRuntimeModifierPipeline,
		pool_size: int = 1,
		graph_config_id: str = "flowx",
		continuation: GraphSpec | None = None,
	) -> None:
		"""Build the merged graph and initialize the native engine part exactly once.

		Subclasses that only learn their business DAG later (``DynamicWorkflowOrchestrator``
		reads it from workflow JSON) call this directly instead of ``__init__``; every
		rebuild after it goes through ``update``.
		"""
		nodes = [dict(node) for node in business_nodes]
		graph, finalize_ids, settings = _build_expanded_graph(
			business_nodes=nodes,
			generation=generation,
			modifier=modifier,
		)
		graph = _with_continuation(graph, continuation, finalize_ids, nodes)
		self.generation = generation
		self.modifier = modifier
		self.business_nodes = nodes
		self._finalize_ids = dict(finalize_ids)
		self._native_settings = settings
		self._continuation = continuation
		with node_settings(settings):
			self._init_from_graph_spec(
				graph,
				graph_config_id=graph_config_id,
				pool_size=pool_size,
			)
	@property
	def finalize_ids(self) -> dict[str, str]:
		"""Business node name to its finalizer step id for the current graph."""
		return dict(self._finalize_ids)

	def update(
		self,
		*,
		business_nodes: list[dict[str, Any]],
		generation: NodeGenerationWorkflow | None = None,
		modifier: NodeRuntimeModifierPipeline | None = None,
		pool_size: int | None = None,
	) -> dict[str, str]:
		"""Update this engine from the difference between the current and new DAG.

		Unchanged business-node chains keep their existing graph classes and
		metadata.  Only added, removed or changed chains are rebuilt; the final
		graph is then reloaded through the existing pool.  ``generation`` and
		``modifier`` default to the workflows this engine was created with.  When
		they change, every chain is rebuilt because their native classes/settings
		changed.
		"""
		if self.closed:
			raise RuntimeError(self.CLOSED_MESSAGE)
		if pool_size is not None and (
			isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1
		):
			raise ValueError("pool_size must be a positive integer or None")
		generation = self.generation if generation is None else generation
		modifier = self.modifier if modifier is None else modifier
		new_nodes = [dict(node) for node in business_nodes]
		_validate_business_nodes(new_nodes)
		new_names = [node["name"] for node in new_nodes]

		workflow_changed = generation is not self.generation or modifier is not self.modifier
		added, removed, changed = _business_graph_diff(self.business_nodes, new_nodes)
		new_name_set = set(new_names)
		descendants = _business_descendants(self.business_nodes, removed.union(changed))
		affected_new = added.union(changed).union(
			name for name in descendants
			if name in new_name_set and name not in removed
		)
		if (
			not workflow_changed
			and not removed
			and not affected_new
			and pool_size in (None, self.pool.size)
		):
			self.business_nodes = new_nodes
			return self.finalize_ids

		if workflow_changed or self._continuation is not None:
			# A continuation is bridged from every leaf finalizer, so its edges cannot
			# be diffed per business node: rebuild the whole graph instead.
			graph, finalize_ids, settings = _build_expanded_graph(
				business_nodes=new_nodes,
				generation=generation,
				modifier=modifier,
			)
			graph = _with_continuation(graph, self._continuation, finalize_ids, new_nodes)
		else:
			finalize_ids = {name: f"{name}__finalize" for name in new_names}
			base = _strip_expanded_chains(self.graph, removed.union(affected_new))
			if affected_new:
				graph, _, settings = _build_expanded_graph(
					business_nodes=[
						node for node in new_nodes if node["name"] in affected_new
					],
					generation=generation,
					modifier=modifier,
					finalize_ids=finalize_ids,
					parent_finalizers={
						node["name"]: [
							finalize_ids[parent]
							for parent in node.get("depends", [])
							if parent in finalize_ids
						]
						for node in new_nodes
					},
					all_business_names=new_names,
					settings_node_names=new_names,
					base_graph=base,
				)
			else:
				graph = base
				settings = self._native_settings

		with node_settings(settings):
			self.reload_graph_spec(graph, pool_size=pool_size)
		self.generation = generation
		self.modifier = modifier
		self.business_nodes = new_nodes
		self._finalize_ids = dict(finalize_ids)
		self._native_settings = settings
		return dict(finalize_ids)


def build_expanded_engine(
	*,
	business_nodes: list[dict[str, Any]],
	generation: NodeGenerationWorkflow,
	modifier: NodeRuntimeModifierPipeline,
	pool_size: int = 1,
	graph_config_id: str = "flowx",
	continuation: GraphSpec | None = None,
) -> tuple[ExpandedWorkflowEngine, dict[str, str]]:
	"""Backward-compatible constructor for :class:`ExpandedWorkflowEngine`."""
	engine = ExpandedWorkflowEngine(
		business_nodes=business_nodes,
		generation=generation,
		modifier=modifier,
		pool_size=pool_size,
		graph_config_id=graph_config_id,
		continuation=continuation,
	)
	return engine, engine.finalize_ids


__all__ = [
	"NAMESPACE_KEY",
	"ExpandedWorkflowEngine",
	"NodeBuildNamespace",
	"NodeRepairInput",
	"build_expanded_engine",
]
