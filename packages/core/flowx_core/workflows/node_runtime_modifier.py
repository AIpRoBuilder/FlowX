"""Repair one generated workflow node from its isolated pytest result.

Modified: exact concrete class maps and construction-scoped instance settings.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ag_ui_workflow import RunResult, StepRunOutput, WorkflowEngine

from flowx_core.runtime import RunNodeTest
from flowx_core.tools._node_settings import apply_node_settings, node_settings
from flowx_core.tools.file_tools import parse_json_file
from flowx_core.tools.workflow_engine import (
	close_workflow_engine,
	init_workflow_engine,
	run_workflow,
)
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


class NodeRuntimeModifierContext:
	"""Shared paths and runtime settings for a single-node repair run."""

	def __init__(
		self,
		node_file_name: str = "",
		workflow_graph: str = "",
		test_file_name: str = "",
		log_file_name: str = "",
		working_directory: str = "",
		python_command: str = "",
		timeout: int = 60,
		skip_generate_node_test: bool = False,
		skip_run_node_test: bool = False,
		skip_amend_node: bool = False,
		workflow_name: str = "",
		node_name: str = "",
		test_temperature: float = 0.2,
		amendment_temperature: float = 0.3,
	) -> None:
		self.workflow_name = workflow_name
		self.node_name = node_name
		self.test_temperature = test_temperature
		self.amendment_temperature = amendment_temperature
		self.node_file_name = node_file_name
		self.workflow_graph = workflow_graph
		self.test_file_name = test_file_name
		self.log_file_name = log_file_name
		self.working_directory = working_directory
		self.python_command = python_command
		self.timeout = timeout
		self.skip_generate_node_test = skip_generate_node_test
		self.skip_run_node_test = skip_run_node_test
		self.skip_amend_node = skip_amend_node
		self.test_file_path = ""
		self.log_file_path = ""
		self.test_log = ""
		self.test_result: dict[str, Any] | None = None
		self.amended_node_file_path = ""
		self.skipped_stages: list[str] = []
		self.completed_stages: set[str] = set()


def _context_from_state(session_state: Mapping[str, Any]) -> NodeRuntimeModifierContext:
	context = session_state.get("node_runtime_modifier")
	if not isinstance(context, NodeRuntimeModifierContext):
		raise TypeError("node runtime modifier context is missing or has an unexpected type")
	return context


def _resolve_path(path_value: str, working_directory: Path) -> Path:
	path = Path(path_value).expanduser()
	return path if path.is_absolute() else working_directory / path


class GenerateNodeTestFile(PromptNodeTestFileCoder):
	"""Generate the single-node pytest module through ``PromptNodeTestFileCoder``."""

	STEP_ID = "generate_node_test"
	DEPENDENCIES = []
	INPUT_REQUIRED = False

	__hash__ = object.__hash__

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		context = _context_from_state(session_state)
		working_directory = Path(context.working_directory).expanduser().resolve()
		node_path = _resolve_path(context.node_file_name, working_directory)
		node_name = context.node_name or node_path.stem
		test_path = _resolve_path(
			context.test_file_name or str(Path("tests") / f"test_{node_name}.py"),
			working_directory,
		)
		if context.skip_generate_node_test:
			context.skipped_stages.append("generate_node_test")
			if not context.skip_run_node_test and not test_path.is_file():
				raise FileNotFoundError(
					f"Skipped node test generation requires an existing test file: {test_path}"
				)
			if test_path.is_file():
				context.test_file_path = str(test_path.resolve())
		else:
			test_path.parent.mkdir(parents=True, exist_ok=True)
			written_path = self.write_test_from_node_file(
				str(node_path),
				str(test_path),
				temperature=context.test_temperature,
			)
			context.test_file_path = str(Path(written_path).resolve())
		context.completed_stages.add("generate_node_test")
		return StepRunOutput(
			card={"kind": "node-test-generation", "status": "completed"},
			derived={"test_file_path": context.test_file_path},
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
		context = _context_from_state(session_state)
		# A slot is exclusively leased to this request. Replace, never mutate,
		# command/result objects already retained by an earlier RunResult.
		working_directory = Path(context.working_directory).expanduser().resolve()
		self.node_name = context.node_name or Path(context.node_file_name).stem
		self.test_path = _resolve_path(
			context.test_file_path or context.test_file_name or str(Path("tests") / f"test_{self.node_name}.py"),
			working_directory,
		).resolve()
		self.log_path = _resolve_path(
			context.log_file_name or str(Path("logs") / f"{self.node_name}_test.log"), working_directory,
		).resolve()
		self.command = [context.python_command or sys.executable, "-m", "pytest", str(self.test_path), "-q"]
		self.cwd = str(working_directory)
		self.timeout = context.timeout
		self.result = None
		context.test_file_path = str(self.test_path)
		context.test_result = None
		context.test_log = ""
		context.log_file_path = ""
		if context.skip_run_node_test:
			context.skipped_stages.append("run_node_test")
			if not context.skip_amend_node:
				if not self.log_path.is_file():
					raise FileNotFoundError(
						f"Skipped node test execution requires an existing log file: {self.log_path}"
					)
				context.log_file_path = str(self.log_path.resolve())
				context.test_log = self.log_path.read_text(encoding="utf-8")
		else:
			status = self._execute()
			if status.isErr() or self.result is None:
				raise RuntimeError(status.getInfo())
			context.log_file_path = str(self.log_path.resolve())
			context.test_log = self.log_path.read_text(encoding="utf-8")
			context.test_result = self.result
		context.completed_stages.add("run_node_test")
		derived = dict(context.test_result or {})
		derived["log_file_path"] = context.log_file_path
		return StepRunOutput(
			card={"kind": "node-test", "status": "completed"},
			derived=derived,
		)


class AmendNodeFromTestLog(PromptNodeFileCoderBase):
	"""Apply the pytest log to the original node with graph-aware node context."""

	STEP_ID = "amend_node"
	DEPENDENCIES = ["run_node_test"]
	INPUT_REQUIRED = False

	__hash__ = object.__hash__

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
		del user_input, dependency_results
		context = _context_from_state(session_state)
		working_directory = Path(context.working_directory).expanduser().resolve()
		node_path = _resolve_path(context.node_file_name, working_directory)
		node_name = context.node_name or node_path.stem
		if context.skip_amend_node or (context.test_result is not None and context.test_result["ok"]):
			context.skipped_stages.append("amend_node")
			context.amended_node_file_path = str(node_path.resolve())
		else:
			if not context.test_log:
				raise RuntimeError("node test output log is missing")
			graph_path = _resolve_path(context.workflow_graph, working_directory)
			if not graph_path.is_file():
				raise FileNotFoundError(f"Workflow graph not found: {graph_path}")
			amended_path = self.amend_code_with_feedback(
				str(node_path),
				context.test_log,
				graph_plan_path=str(graph_path),
				current_node_name=node_name,
				temperature=context.amendment_temperature,
			)
			context.amended_node_file_path = str(Path(amended_path).resolve())
		context.completed_stages.add("amend_node")
		return StepRunOutput(
			card={"kind": "node-amendment", "status": "completed"},
			derived={"amended_node_file_path": context.amended_node_file_path},
		)


@dataclass
class NodeRuntimeModifierPipeline:
	"""Workflow engine for test-driven repair of one workflow node."""

	node_test_writer: GenerateNodeTestFile
	node_writer: AmendNodeFromTestLog
	run_subprocess: Callable[..., Any] = subprocess.run
	timeout_expired: type[BaseException] = subprocess.TimeoutExpired
	_engine: WorkflowEngine | None = field(default=None, init=False, repr=False)
	_last_result: RunResult | None = field(default=None, init=False, repr=False)

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
			self._engine = init_workflow_engine(NODE_CLASSES, self.json_config())

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

	def close(self) -> None:
		"""Release the native pool when this modifier is no longer needed."""
		if self._engine is not None:
			close_workflow_engine(self._engine)

	def run(
		self,
		*,
		node_file_name: str,
		workflow_graph: str | Mapping[str, Any],
		working_directory: str,
		test_file_name: str = "",
		log_file_name: str = "",
		python_command: str = "",
		timeout: int = 60,
		skip_generate_node_test: bool = False,
		skip_run_node_test: bool = False,
		skip_amend_node: bool = False,
		workflow_name: str = "",
		node_name: str = "",
		test_temperature: float = 0.2,
		amendment_temperature: float = 0.3,
	) -> NodeRuntimeModifierContext:
		"""Generate, test, and amend one node; return paths, logs, and test data."""

		workdir = Path(working_directory).expanduser().resolve()
		if not workdir.is_dir():
			raise NotADirectoryError(f"Working directory not found: {workdir}")
		if skip_generate_node_test and not skip_run_node_test:
			test_path = _resolve_path(
				test_file_name or str(Path("tests") / f"test_{node_name or Path(node_file_name).stem}.py"),
				workdir,
			)
			if not test_path.is_file():
				raise FileNotFoundError(f"Skipped node test generation requires an existing test file: {test_path}")

		context = NodeRuntimeModifierContext(
			node_file_name=node_file_name,
			workflow_graph=str(workflow_graph),
			test_file_name=test_file_name,
			log_file_name=log_file_name,
			working_directory=str(workdir),
			python_command=python_command,
			timeout=timeout,
			skip_generate_node_test=skip_generate_node_test,
			skip_run_node_test=skip_run_node_test,
			skip_amend_node=skip_amend_node,
			workflow_name=workflow_name,
			node_name=node_name,
			test_temperature=test_temperature,
			amendment_temperature=amendment_temperature,
		)
		self._last_result = None
		if isinstance(workflow_graph, Mapping):
			# Keep mapping-backed graphs request-local even when repairs share a
			# workdir. Retain the snapshot for the returned context's provenance.
			graph_path = workdir / ".flowx" / "runtime_modifier" / uuid.uuid4().hex / "workflow.json"
			graph_path.parent.mkdir(parents=True, exist_ok=True)
			graph_path.write_text(json.dumps(dict(workflow_graph), ensure_ascii=False, indent=2), encoding="utf-8")
			context.workflow_graph = str(graph_path)
		if self._engine is None:
			raise RuntimeError("node runtime modifier engine is not initialized")
		self._last_result = run_workflow(
			self._engine, session_state={"node_runtime_modifier": context},
		)
		if not skip_run_node_test and context.test_result is None:
			raise RuntimeError(f"node test execution did not produce a result for {context.node_name or node_file_name}")
		return context


__all__ = [
	"NodeRuntimeModifierContext",
	"GenerateNodeTestFile",
	"ExecuteNodeTest",
	"AmendNodeFromTestLog",
	"NodeRuntimeModifierPipeline",
]
