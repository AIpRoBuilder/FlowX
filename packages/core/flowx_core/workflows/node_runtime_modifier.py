"""Repair one generated workflow node from its isolated pytest result.

Modified: exact concrete class maps and construction-scoped instance settings.
Modified: immutable request inputs, request-local execution data and terminal close.
Modified 2026-10-14: native-free request execution; pooled nodes keep only dependencies.
Modified 2026-10-15: mutable repair requests own outputs; each run uses a fresh copy.
Modified 2026-10-15: remove unused workflow name and write-only stage bookkeeping.
Modified 2026-10-20: gate the amendment on user input that guides it or skips it.
Modified 2026-10-20: interactive runs ask for guidance after the node test result.
Modified 2026-10-24: be the WorkflowEngine instead of wrapping one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from ag_ui_workflow import StepRunOutput

from flowx_core.runtime import RunNodeTest
from flowx_core.runtime.run_node_test import _execute_node_test
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.tools.file_tools import parse_json_file
from flowx_core.tools.workflow_engine import (
	_RUN_REQUEST,
	FlowXWorkflowEngine,
)
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


# Exact, case-insensitive inputs that skip the amendment instead of guiding it.
_SKIP_AMENDMENT_INPUTS = frozenset({
	"skip", "/skip", "skip amendment", "skip_amendment", "skip-amendment",
})


def _amendment_input_parts(user_input: Any) -> tuple[bool, str]:
	"""Split user input into (skip amendment, guidance), accepting text or JSON.

	The workflow engine normalizes object inputs to JSON text, so ``{"skip": true}``
	and ``{"guidance": "..."}`` are accepted alongside the plain-text forms.
	"""
	if isinstance(user_input, Mapping):
		payload: Mapping[str, Any] | None = user_input
		text = ""
	else:
		text = str(user_input or "").strip()
		payload = None
		if text.startswith("{"):
			try:
				parsed = json.loads(text)
			except (TypeError, ValueError):
				parsed = None
			if isinstance(parsed, Mapping):
				payload = parsed
	if payload is None:
		skip = text.lower() in _SKIP_AMENDMENT_INPUTS
		return skip, "" if skip else text
	skip = bool(payload.get("skip", payload.get("skip_amendment", False)))
	recognized = "skip" in payload or "skip_amendment" in payload
	for key in ("guidance", "amendment", "input", "text"):
		if key in payload:
			recognized = True
			guidance = str(payload.get(key) or "").strip()
			if guidance:
				return skip, guidance
			break
	return skip, "" if recognized or not payload else json.dumps(dict(payload), ensure_ascii=False)


def _compose_amendment(test_log: str, guidance: str) -> str:
	"""Append authoritative user guidance to the test log without changing the log alone."""
	guidance = guidance.strip()
	if not guidance:
		return test_log
	if not test_log.strip():
		return guidance
	return (
		f"{test_log.rstrip()}\n\n"
		"User guidance for this amendment (authoritative):\n"
		f"{guidance}\n"
	)


@dataclass(kw_only=True)
class NodeRuntimeModifierRequest:
	"""Repair inputs and mutable outputs, shallow-copied with fresh outputs per run."""

	node_file_name: str
	workflow_graph: str | Mapping[str, Any]
	working_directory: str
	test_file_name: str = ""
	log_file_name: str = ""
	python_command: str = ""
	timeout: int = 60
	skip_generate_node_test: bool = False
	skip_run_node_test: bool = False
	skip_amend_node: bool = False
	node_name: str = ""
	test_temperature: float = 0.2
	amendment_temperature: float = 0.3
	state: Mapping[str, Any] = field(default_factory=dict)
	test_file_path: str = field(default="", init=False)
	log_file_path: str = field(default="", init=False)
	test_log: str = field(default="", init=False)
	test_result: dict[str, Any] | None = field(default=None, init=False)
	amended_node_file_path: str = field(default="", init=False)


def _request_from_state(session_state: Mapping[str, Any]) -> NodeRuntimeModifierRequest:
	request = session_state.get("node_runtime_modifier")
	if not isinstance(request, NodeRuntimeModifierRequest):
		raise TypeError("node runtime modifier request is missing or has an unexpected type")
	return request


def _resolve_path(path_value: str, working_directory: Path) -> Path:
	path = Path(path_value).expanduser()
	return path if path.is_absolute() else working_directory / path


class GenerateNodeTestFile(PromptNodeTestFileCoder):
	"""Generate the single-node pytest module through ``PromptNodeTestFileCoder``."""

	STEP_ID = "generate_node_test"
	DEPENDENCIES = []
	INPUT_REQUIRED = False

	__hash__ = object.__hash__

	def _build_user_prompt(self, node_file_path: str) -> str:
		prompt = super()._build_user_prompt(node_file_path)
		request = _request_from_state(self.get_workflow_param().state)
		working_directory = Path(request.working_directory).expanduser().resolve()
		node_path = _resolve_path(node_file_path, working_directory).resolve()
		return (
			prompt
			+ "\nRequest working directory (authoritative):\n"
			+ f"- working_directory: {working_directory}\n"
			+ f"- Load this node with Path({str(node_path)!r}), regardless of the test output directory.\n"
			+ "- Resolve request-relative paths against this working directory, not a construction-time root.\n"
		)

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		request = _request_from_state(session_state)
		working_directory = Path(request.working_directory).expanduser().resolve()
		node_path = _resolve_path(request.node_file_name, working_directory)
		node_name = request.node_name or node_path.stem
		test_path = _resolve_path(
			request.test_file_name or str(Path("tests") / f"test_{node_name}.py"),
			working_directory,
		)
		if request.skip_generate_node_test:
			if not request.skip_run_node_test and not test_path.is_file():
				raise FileNotFoundError(
					f"Skipped node test generation requires an existing test file: {test_path}"
				)
			if test_path.is_file():
				request.test_file_path = str(test_path.resolve())
		else:
			test_path.parent.mkdir(parents=True, exist_ok=True)
			written_path = self.write_test_from_node_file(
				str(node_path),
				str(test_path),
				temperature=request.test_temperature,
			)
			request.test_file_path = str(Path(written_path).resolve())
		return StepRunOutput(
			card={"kind": "node-test-generation", "status": "completed"},
			derived={"test_file_path": request.test_file_path},
		)


class ExecuteNodeTest(RunNodeTest):
	"""Run the generated pytest module and retain its dedicated log."""

	STEP_ID = "run_node_test"
	DEPENDENCIES = ["generate_node_test"]
	INPUT_REQUIRED = False

	def __init__(
		self,
		*,
		run_subprocess: Callable[..., Any] = subprocess.run,
		timeout_expired: type[BaseException] = subprocess.TimeoutExpired,
	) -> None:
		"""Retain execution dependencies, not one request's paths or command."""
		super().__init__(
			node_name="", test_path=Path(), log_path=Path(), command=[], cwd="", timeout=60,
			run_subprocess=run_subprocess, timeout_expired=timeout_expired,
		)
		apply_node_settings(self)

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		request = _request_from_state(session_state)
		# The pooled step retains only execution dependencies. The standalone
		# function's paths, command and result belong exclusively to this request.
		working_directory = Path(request.working_directory).expanduser().resolve()
		node_name = request.node_name or Path(request.node_file_name).stem
		test_path = _resolve_path(
			request.test_file_path or request.test_file_name or str(Path("tests") / f"test_{node_name}.py"),
			working_directory,
		).resolve()
		log_path = _resolve_path(
			request.log_file_name or str(Path("logs") / f"{node_name}_test.log"), working_directory,
		).resolve()
		request.test_file_path = str(test_path)
		request.test_result = None
		request.test_log = ""
		request.log_file_path = ""
		if request.skip_run_node_test:
			if not request.skip_amend_node:
				if not log_path.is_file():
					raise FileNotFoundError(
						f"Skipped node test execution requires an existing log file: {log_path}"
					)
				request.log_file_path = str(log_path)
				request.test_log = log_path.read_text(encoding="utf-8")
		else:
			result = _execute_node_test(
				node_name=node_name, test_path=test_path, log_path=log_path,
				command=[request.python_command or sys.executable, "-m", "pytest", str(test_path), "-q"],
				cwd=str(working_directory), timeout=request.timeout,
				run_subprocess=self.run_subprocess, timeout_expired=self.timeout_expired,
			)
			if result is None:
				raise RuntimeError("node test execution did not produce a result")
			request.log_file_path = str(log_path)
			request.test_log = log_path.read_text(encoding="utf-8")
			request.test_result = result
		derived = dict(request.test_result or {})
		derived["log_file_path"] = request.log_file_path
		return StepRunOutput(
			card={"kind": "node-test", "status": "completed"},
			derived=derived,
		)


class AmendNodeFromTestLog(PromptNodeFileCoderBase):
	"""Apply the pytest log and optional user guidance, or skip the amendment on request."""

	STEP_ID = "amend_node"
	DEPENDENCIES = ["run_node_test"]
	INPUT_REQUIRED = True

	__hash__ = object.__hash__

	def __post_init__(self) -> None:
		"""Install the input gate after the base coder prepares the workflow node."""
		super().__post_init__()
		self.setWaitFunc(self.wait_function)

	def wait_function(self) -> bool:
		"""Pause until the caller supplies guidance, a skip instruction, or a blank proceed."""
		param = self.get_workflow_param()
		with param.condition:
			if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
				return True
			request = param.state.get("node_runtime_modifier")
			if not isinstance(request, NodeRuntimeModifierRequest):
				param.set_state(self.STEP_ID, "running")
				return True
			if request.skip_amend_node or (request.test_result is not None and request.test_result["ok"]):
				param.set_state(self.STEP_ID, "running")
				return True
			ready = self.STEP_ID in param.inputs
			param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
			return ready

	def get_node_contract_text(self) -> str:
		return ""

	def get_feedback_contract_text(self) -> str:
		return "Preserve the existing AG-UI workflow node contract while applying the test feedback.\n"

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del dependency_results
		request = _request_from_state(session_state)
		working_directory = Path(request.working_directory).expanduser().resolve()
		node_path = _resolve_path(request.node_file_name, working_directory)
		node_name = request.node_name or node_path.stem
		skip_input, guidance = _amendment_input_parts(user_input)
		if skip_input:
			request.skip_amend_node = True
		if request.skip_amend_node or (request.test_result is not None and request.test_result["ok"]):
			request.amended_node_file_path = str(node_path.resolve())
		else:
			if not request.test_log:
				raise RuntimeError("node test output log is missing")
			graph_path = _resolve_path(request.workflow_graph, working_directory)
			if not graph_path.is_file():
				raise FileNotFoundError(f"Workflow graph not found: {graph_path}")
			amended_path = self.amend_code_with_feedback(
				str(node_path),
				_compose_amendment(request.test_log, guidance),
				graph_plan_path=str(graph_path),
				current_node_name=node_name,
				temperature=request.amendment_temperature,
			)
			request.amended_node_file_path = str(Path(amended_path).resolve())
		return StepRunOutput(
			card={"kind": "node-amendment", "status": "completed"},
			derived={"amended_node_file_path": request.amended_node_file_path},
		)


@dataclass
class NodeRuntimeModifierPipeline(FlowXWorkflowEngine):
	"""Workflow engine for test-driven repair of one workflow node."""

	node_test_writer: GenerateNodeTestFile
	node_writer: AmendNodeFromTestLog
	run_subprocess: Callable[..., Any] = subprocess.run
	timeout_expired: type[BaseException] = subprocess.TimeoutExpired
	_closed: bool = field(default=False, init=False, repr=False)
	REQUEST_TYPES = (NodeRuntimeModifierRequest,)
	CLOSED_MESSAGE = "node runtime modifier is closed or not initialized"

	def __post_init__(self) -> None:
		# Eager pool construction consumes shallow snapshots, preserving client
		# and recording-sink identities. Request paths/settings remain in state.
		NODES = (type(self.node_test_writer), ExecuteNodeTest, type(self.node_writer))
		NODE_CLASSES = {node.STEP_ID: node for node in NODES}
		settings = {
			component.STEP_ID: {
				setting.name: getattr(component, setting.name)
				for setting in fields(component) if setting.init
			}
			for component in (self.node_test_writer, self.node_writer)
			if is_dataclass(component)
		}
		settings[ExecuteNodeTest.STEP_ID] = {
			"run_subprocess": self.run_subprocess,
			"timeout_expired": self.timeout_expired,
		}
		with node_settings(settings):
			super().__init__(NODE_CLASSES, self.json_config())

	@staticmethod
	def json_config() -> dict[str, Any]:
		"""Return the topology and pool_size for the three repair stages."""

		config = parse_json_file(
			"node_runtime_modifier.json",
			Path(__file__).with_name("configs"),
		)
		if not isinstance(config, dict):
			raise TypeError("Node runtime modifier configuration must be a JSON object")
		return config

	def _prepare_request(self, request: NodeRuntimeModifierRequest) -> NodeRuntimeModifierRequest:
		"""Resolve request-local paths and snapshot mapping-backed graphs."""
		workdir = Path(request.working_directory).expanduser().resolve()
		if not workdir.is_dir():
			raise NotADirectoryError(f"Working directory not found: {workdir}")
		# replace is shallow for inputs/state and reconstructs every init=False
		# output, so repeated/concurrent runs cannot reuse caller-owned results.
		request = replace(request, working_directory=str(workdir))
		if request.skip_generate_node_test and not request.skip_run_node_test:
			test_path = _resolve_path(
				request.test_file_name or str(Path("tests") / f"test_{request.node_name or Path(request.node_file_name).stem}.py"),
				workdir,
			)
			if not test_path.is_file():
				raise FileNotFoundError(f"Skipped node test generation requires an existing test file: {test_path}")

		if isinstance(request.workflow_graph, Mapping):
			# Keep mapping-backed graphs request-local even when repairs share a
			# workdir. Retain the snapshot for the returned request's provenance.
			graph_path = workdir / ".flowx" / "runtime_modifier" / uuid.uuid4().hex / "workflow.json"
			graph_path.parent.mkdir(parents=True, exist_ok=True)
			graph_path.write_text(json.dumps(dict(request.workflow_graph), ensure_ascii=False, indent=2), encoding="utf-8")
			request.workflow_graph = str(graph_path)
		return request

	def execute(
		self,
		request: NodeRuntimeModifierRequest,
		user_input: str = "",
	) -> NodeRuntimeModifierRequest:
		"""Return a fresh repair request with outputs; leave caller inputs unchanged.

		``user_input`` is fed to the gated amendment step: non-empty text guides the
		amendment alongside the test log, exact skip values (``skip``, ``/skip``,
		``skip amendment``, ``skip_amendment`` or ``skip-amendment``) skip it, and
		an empty value proceeds from the test log alone.
		"""

		request = self._prepare_request(request)
		skip_amendment, _ = _amendment_input_parts(user_input)
		if skip_amendment:
			# An explicit skip also avoids requiring an existing log when test
			# execution is skipped for this request.
			request.skip_amend_node = True
		self.run_steps(
			state={**request.state, "node_runtime_modifier": request},
			inputs={AmendNodeFromTestLog.STEP_ID: user_input},
		)
		if not request.skip_run_node_test and request.test_result is None:
			raise RuntimeError(f"node test execution did not produce a result for {request.node_name or request.node_file_name}")
		return request

	def run_interactive(
		self,
		request: NodeRuntimeModifierRequest,
		input_callback: Callable[[NodeRuntimeModifierRequest], str],
	) -> NodeRuntimeModifierRequest:
		"""Run the repair pipeline, pausing for input after a failing node test.

		The callback receives the run-local request after ``run_node_test`` has set
		``test_result`` and ``test_log``. Return guidance, ``"skip"`` to skip the
		amendment, or an empty string to amend from the test log alone.
		"""

		if self.closed:
			raise RuntimeError(self.CLOSED_MESSAGE)
		request = self._prepare_request(request)
		request_id = f"flowx-request-{uuid.uuid4().hex}"
		state = {**request.state, "node_runtime_modifier": request}
		token = _RUN_REQUEST.set((state, frozenset()))
		try:
			result = self.run(request_id)
		finally:
			_RUN_REQUEST.reset(token)
		try:
			while result.status == "awaiting_input":
				if result.awaiting["id"] != AmendNodeFromTestLog.STEP_ID:
					raise RuntimeError(f"unexpected workflow input gate: {result.awaiting['id']}")
				answer = input_callback(request)
				result = self.answer(request_id, answer)
			if result.status != "completed":
				raise RuntimeError(result.error or result.status)
			if not request.skip_run_node_test and request.test_result is None:
				raise RuntimeError(f"node test execution did not produce a result for {request.node_name or request.node_file_name}")
			return request
		finally:
			self.close(request_id)


__all__ = [
	"NodeRuntimeModifierRequest",
	"GenerateNodeTestFile",
	"ExecuteNodeTest",
	"AmendNodeFromTestLog",
	"NodeRuntimeModifierPipeline",
]
