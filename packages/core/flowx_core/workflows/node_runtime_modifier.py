"""Repair one generated workflow node from its isolated pytest result."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ag_ui_workflow import WorkflowOperationNode
from pydaograph import CStatus, GParam, GPipeline

from flowx_core.runtime import RunNodeTest
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase


class NodeRuntimeModifierContext(GParam):
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
	) -> None:
		super().__init__()
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


def _context_from_node(node: WorkflowOperationNode) -> NodeRuntimeModifierContext:
	context = node.getGParam(NodeRuntimeModifierPipeline.CONTEXT_KEY)
	if not isinstance(context, NodeRuntimeModifierContext):
		raise TypeError("node runtime modifier context is missing or has an unexpected type")
	return context


def _resolve_path(path_value: str, working_directory: Path) -> Path:
	path = Path(path_value).expanduser()
	return path if path.is_absolute() else working_directory / path


class GenerateNodeTestFile(PromptNodeTestFileCoder):
	"""Generate the single-node pytest module through ``PromptNodeTestFileCoder``."""

	__hash__ = object.__hash__

	def run(self) -> CStatus:
		try:
			context = _context_from_node(self)

			working_directory = Path(context.working_directory).expanduser().resolve()
			node_path = _resolve_path(context.node_file_name, working_directory)
			test_path = _resolve_path(
				context.test_file_name or str(Path("tests") / f"test_{node_path.stem}.py"),
				working_directory,
			)
			if context.skip_generate_node_test:
				context.skipped_stages.append("generate_node_test")
				if context.skip_run_node_test:
					return CStatus()
				if not test_path.is_file():
					raise FileNotFoundError(
						f"Skipped node test generation requires an existing test file: {test_path}"
					)
				context.test_file_path = str(test_path.resolve())
				return CStatus()

			test_path.parent.mkdir(parents=True, exist_ok=True)
			written_path = self.write_test_from_node_file(str(node_path), str(test_path))
			context.test_file_path = str(Path(written_path).resolve())
			return CStatus()
		except Exception as exc:
			return CStatus(1001, f"generate node test file failed: {exc}")


class ExecuteNodeTest(RunNodeTest):
	"""Run the generated pytest module and retain its dedicated log."""

	def run(self) -> CStatus:
		try:
			context = _context_from_node(self)
			if context.skip_run_node_test:
				context.skipped_stages.append("run_node_test")
				if context.skip_amend_node:
					return CStatus()
				if not self.log_path.is_file():
					raise FileNotFoundError(
						f"Skipped node test execution requires an existing log file: {self.log_path}"
					)
				context.log_file_path = str(self.log_path.resolve())
				context.test_log = self.log_path.read_text(encoding="utf-8")
				return CStatus()

			status = super().run()
			if status.isErr() or self.result is None:
				raise RuntimeError(status.getInfo())

			context.log_file_path = str(self.log_path.resolve())
			context.test_log = self.log_path.read_text(encoding="utf-8")
			context.test_result = self.result
			return CStatus()
		except Exception as exc:
			return CStatus(1001, f"run node test failed: {exc}")


class AmendNodeFromTestLog(PromptNodeFileCoderBase):
	"""Apply the pytest log to the original node with graph-aware node context."""

	__hash__ = object.__hash__

	def get_node_contract_text(self) -> str:
		return ""

	def get_feedback_contract_text(self) -> str:
		return "Preserve the existing AG-UI workflow node contract while applying the test feedback.\n"

	def run(self) -> CStatus:
		try:
			context = _context_from_node(self)

			working_directory = Path(context.working_directory).expanduser().resolve()
			node_path = _resolve_path(context.node_file_name, working_directory)
			if context.skip_amend_node:
				context.skipped_stages.append("amend_node")
				context.amended_node_file_path = str(node_path.resolve())
				return CStatus()

			if not context.test_log:
				raise RuntimeError("node test output log is missing")

			graph_path = _resolve_path(context.workflow_graph, working_directory)
			if not graph_path.is_file():
				raise FileNotFoundError(f"Workflow graph not found: {graph_path}")

			amended_path = self.amend_code_with_feedback(
				str(node_path),
				context.test_log,
				graph_plan_path=str(graph_path),
				current_node_name=node_path.stem,
			)
			context.amended_node_file_path = str(Path(amended_path).resolve())
			return CStatus()
		except Exception as exc:
			return CStatus(1001, f"amend node from test log failed: {exc}")


@dataclass
class NodeRuntimeModifierPipeline:
	"""Workflow operation pipeline for test-driven repair of one workflow node."""

	node_test_writer: GenerateNodeTestFile
	node_writer: AmendNodeFromTestLog
	run_subprocess: Callable[..., Any] = subprocess.run
	timeout_expired: type[BaseException] = subprocess.TimeoutExpired
	pipeline_factory: Callable[[], GPipeline] = GPipeline
	_pipeline: GPipeline | None = field(default=None, init=False, repr=False)

	CONTEXT_KEY = "flowx.node_runtime_modifier.context"

	@staticmethod
	def json_config() -> dict[str, Any]:
		"""Return the topology definition for the three repair stages."""

		return {
			"nodes": [
				{
					"name": "generate_node_test",
					"type": "GenerateNodeTestFile",
					"meta_type": "WorkflowOperationNode",
					"loop": 1,
				},
				{
					"name": "run_node_test",
					"type": "ExecuteNodeTest",
					"meta_type": "WorkflowOperationNode",
					"depends": ["generate_node_test"],
					"loop": 1,
				},
				{
					"name": "amend_node",
					"type": "AmendNodeFromTestLog",
					"meta_type": "WorkflowOperationNode",
					"depends": ["run_node_test"],
					"loop": 1,
				},
			]
		}

	def _build_pipeline(self, context: NodeRuntimeModifierContext) -> GPipeline:
		"""Build the pipeline from configured workflow operation nodes.

		The writer subclasses require LLM settings, so register the configured
		instances directly using the names and edges in :meth:`json_config`.
		"""

		config = self.json_config()
		pipeline = self.pipeline_factory()

		working_directory = Path(context.working_directory)
		node_path = _resolve_path(context.node_file_name, working_directory)
		test_path = _resolve_path(
			context.test_file_name or str(Path("tests") / f"test_{node_path.stem}.py"),
			working_directory,
		)
		log_path = _resolve_path(
			context.log_file_name or str(Path("logs") / f"{node_path.stem}_test.log"),
			working_directory,
		)
		context.test_file_path = str(test_path.resolve())
		elements: dict[str, WorkflowOperationNode] = {
			"generate_node_test": self.node_test_writer,
			"run_node_test": ExecuteNodeTest(
				node_name=node_path.stem,
				test_path=test_path,
				log_path=log_path,
				command=[context.python_command or sys.executable, "-m", "pytest", str(test_path), "-q"],
				cwd=str(working_directory),
				timeout=context.timeout,
				run_subprocess=self.run_subprocess,
				timeout_expired=self.timeout_expired,
			),
			"amend_node": self.node_writer,
		}

		for node_config in config["nodes"]:
			node_name = str(node_config["name"])
			dependencies = {
				elements[str(dependency)]
				for dependency in node_config.get("depends", [])
			}
			registration_status = pipeline.registerGElement(
				elements[node_name],
				dependencies,
				node_name,
				int(node_config.get("loop", 1)),
			)
			if registration_status.isErr():
				raise RuntimeError(
					f"register node runtime modifier stage '{node_name}' failed: "
					f"{registration_status.getInfo()}"
				)

		return pipeline

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
	) -> NodeRuntimeModifierContext:
		"""Generate, test, and amend one node; return paths, logs, and test data."""

		workdir = Path(working_directory).expanduser().resolve()
		if not workdir.is_dir():
			raise NotADirectoryError(f"Working directory not found: {workdir}")

		if isinstance(workflow_graph, Mapping):
			graph_path = workdir / "workflow.json"
			graph_path.write_text(json.dumps(dict(workflow_graph), ensure_ascii=False, indent=2), encoding="utf-8")
			workflow_graph = str(graph_path)

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
		)
		pipeline = self._build_pipeline(context)
		pipeline.createGParam(context, self.CONTEXT_KEY)
		status = pipeline.process()
		if status.isErr():
			raise RuntimeError(f"node runtime modifier pipeline failed: {status.getInfo()}")

		self._pipeline = pipeline
		return context


__all__ = [
	"NodeRuntimeModifierContext",
	"GenerateNodeTestFile",
	"ExecuteNodeTest",
	"AmendNodeFromTestLog",
	"NodeRuntimeModifierPipeline",
]
