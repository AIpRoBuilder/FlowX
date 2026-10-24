"""Single-pass main-entrypoint generation using the existing writer and auditor.

Modified 2026-10-14: extract the workflow from MainEntrypointService.
Modified 2026-10-14: reuse a service-owned pool with request-local native state.
Modified 2026-10-15: remove the unused temperature from template-only requests.
Modified 2026-10-24: be the WorkflowEngine instead of wrapping one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.tools._node_settings import node_settings
from flowx_core.tools.file_tools import parse_json_file
from flowx_core.tools.workflow_engine import FlowXWorkflowEngine
from flowx_core.worker.main_writer import PromptMainFileCoder


@dataclass(frozen=True, kw_only=True)
class MainEntrypointGenerationRequest:
	"""Explicit paths and caller-owned state for one entrypoint generation."""

	project_root_path: str
	graph_plan_json_path: str
	output_path: str
	fastapi_host: str = "0.0.0.0"
	fastapi_port: int = 8000
	state: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class MainEntrypointGenerationWorkflow(FlowXWorkflowEngine):
	"""Reuse ``main_generate -> main_audit`` for independent build requests."""

	main_writer: PromptMainFileCoder
	main_entry_auditor: MainEntryPointAuditor
	REQUEST_TYPES = (MainEntrypointGenerationRequest,)
	CLOSED_MESSAGE = "main entrypoint workflow is closed"

	def __post_init__(self) -> None:
		writer, auditor = self.main_writer, self.main_entry_auditor
		node_classes = {type(node).STEP_ID: type(node) for node in (writer, auditor)}
		settings = {
			setting.name: getattr(writer, setting.name)
			for setting in fields(writer) if setting.init
		}
		# This is reusable writer configuration for later explicit amendments,
		# not a service-owned session or a retained workflow run.
		settings["_render_context_by_target"] = writer._render_context_by_target
		with node_settings({writer.STEP_ID: settings}):
			super().__init__(node_classes, self.json_config())

	@staticmethod
	def json_config() -> dict[str, Any]:
		"""Load the writer/auditor topology and pool size from workflow JSON."""
		config = parse_json_file("main_entrypoint.json", Path(__file__).with_name("configs"))
		if not isinstance(config, dict):
			raise TypeError("Main entrypoint configuration must be a JSON object")
		return config

	def execute(
		self,
		request: MainEntrypointGenerationRequest,
	) -> str:
		"""Write and audit the entrypoint, returning its path without retrying."""
		writer = self.main_writer
		auditor = self.main_entry_auditor
		result = self.run_steps(state={
			**request.state,
			f"{type(writer).__name__}::action": "write",
			"main_entrypoint": {
				"project_root_path": request.project_root_path,
				"graph_plan_json_path": request.graph_plan_json_path,
				"output_path": request.output_path,
				"fastapi_host": request.fastapi_host,
				"fastapi_port": request.fastapi_port,
			},
			"auditor": {"file_path": request.output_path, "nodes_root": request.project_root_path},
		})

		audit_result = result.outputs[auditor.STEP_ID].derived
		if not audit_result["ok"]:
			feedback = "\n".join(
				f"Line {item['lineno']}: {item['rule']} - {item['detail']}"
				for item in audit_result["violations"]
			)
			raise RuntimeError(f"main entrypoint audit did not pass. Feedback:\n{feedback}")
		return result.outputs[writer.STEP_ID].derived["main_entrypoint_path"]


__all__ = ["MainEntrypointGenerationRequest", "MainEntrypointGenerationWorkflow"]
