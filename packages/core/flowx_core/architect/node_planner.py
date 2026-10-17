# Modified: use request-local settings and preserve configured pooled-node prompts.
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools._node_settings import apply_node_settings
from flowx_core.tools.workflow_node_reference import (
	render_workflow_method_signatures,
	render_workflow_step_meta_catalog,
	resolve_workflow_node_reference,
	workflow_node_references,
)


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS


def _method_list_text(method_signatures: list[str]) -> str:
	return ", ".join(method_signatures) if method_signatures else "none"


@dataclass
class NodePlanElement(Coder):
	"""Generate a concise markdown implementation brief for a single graph node.

	The brief is derived from:
	- requirement analysis markdown
	- node desc/dependency/ext_data from graph_plan.json
	- ag_ui_workflow base-node step_meta metadata

	Output format is markdown (.md) that lists suggested tools/functions per node.

	This step uses Coder to generate and amend node markdown.
	 """

	STEP_ID = "node_plan"
	DEPENDENCIES = []
	INPUT_REQUIRED = False

	prompt_path: str = "architect/prompts/node_planner_prompt.md"
	output_path: Optional[str] = None
	overwrite: bool = True
	temperature: float = 0.2
	max_tokens: int = MAX_TOKENS

	def __post_init__(self) -> None:
		apply_node_settings(self)
		prompt_file = ROOT_DIR / self.prompt_path
		if not prompt_file.exists():
			raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

		base_prompt = prompt_file.read_text(encoding="utf-8")
		if self.system_prompt == Coder.system_prompt:
			self.system_prompt = (
				f"{base_prompt}\n\n"
				"## ag_ui_workflow Base Step Metas (Authoritative)\n"
				"Use the following base-node `step_meta()` catalog as the source of truth for `meta_node_kind`,\n"
				"node capability boundaries, and default ext_data pairings.\n\n"
				f"{render_workflow_step_meta_catalog()}\n"
			)
		super().__post_init__()

	def _build_node_prompt(self, requirement_text: str, node_context: str, dependencies: str = "") -> str:
		return (
			"Generate a VERY SHORT markdown note for this SINGLE node only.\n"
			"Goal: briefly describe what this node will achieve and list the core functions to implement.\n"
			"Do NOT provide detailed implementation steps, dependency handling details, output contract details, or TODO lists.\n"
			"Input sources to use: requirement analysis + node desc + graph meta_node_kind/ext_data.\n"
			"If the selected base-node contract allows inputs_format and inputs_format is provided, only mention the required validation/parsing at a high level.\n"
			"Node type must align with workflow reference contracts.\n"
			"Keep output concise and practical (MVP-first, no extra features).\n\n"
			"Mandatory output sections:\n"
			"1) # Node Brief\n"
			"2) ## What This Node Achieves (2-4 sentences)\n"
			"3) ## Core Functions (bullet list of function names with one-line purpose each)\n\n"
			"Requirement analysis markdown:\n"
			f"{requirement_text}\n\n"
			"Target node context:\n"
			f"{node_context}\n"
			f"{dependencies}"
		)

	def plan_node(
		self,
		node: dict[str, Any],
		requirement_text: str,
		output_path: str | Path,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
		dependencies: str = "",
	) -> Path:
		"""Write the brief for one node to the requested path."""
		prompt = self._build_node_prompt(requirement_text, self.render_node_context(node), dependencies)
		return self.code_to_file(
			prompt,
			str(output_path),
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)

	def amend_node_plan(
		self,
		node: dict[str, Any],
		requirement_text: str,
		output_path: str | Path,
		amendment: str,
		*,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
		dependencies: str = "",
	) -> Path:
		"""Amend only an existing node brief, retaining its required sections."""
		if not amendment.strip():
			raise ValueError("markdown amendment must be a non-empty string")
		path = Path(output_path)
		if not path.is_file():
			raise FileNotFoundError(f"node markdown file not found: {path}")
		prompt = (
			"Amend only this node's implementation markdown. Keep it concise and retain the required Node Brief sections.\n"
			f"Global requirement analysis:\n{requirement_text}\n\n"
			f"Node context:\n{self.render_node_context(node)}\n\n"
			f"{dependencies}"
			f"Existing node markdown:\n{path.read_text(encoding='utf-8')}\n\n"
			f"Amendment instructions:\n{amendment}\n"
			"Return only the complete amended markdown, without commentary outside the document.\n"
		)
		return self.code_to_file(
			prompt,
			str(path),
			overwrite=True,
			temperature=temperature,
			max_tokens=max_tokens,
		)

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input, dependency_results
		plan_request = session_state.get("node_plan_request")
		if not isinstance(plan_request, dict):
			raise TypeError("node_plan_request must be a dictionary")
		output_path = plan_request.get("output_path", self.output_path)
		if output_path is None:
			raise ValueError("Configure output_path before running the node plan step.")
		temperature = plan_request.get("temperature", self.temperature)
		node = plan_request.get("node")
		if not isinstance(node, dict):
			raise ValueError("node_plan_request requires a node dictionary")
		requirement_text = plan_request.get("requirement_text", "")
		generate_markdown = plan_request.get("generate_markdown", True)
		amendment = plan_request.get("amendment", "")
		dependencies = plan_request.get("dependencies", "")
		if (
			not isinstance(requirement_text, str)
			or not isinstance(generate_markdown, bool)
			or not isinstance(amendment, str)
			or not isinstance(dependencies, str)
		):
			raise TypeError(
				"node_plan_request requires string requirement_text, boolean generate_markdown, "
				"string amendment and string dependencies"
			)
		path = Path(output_path)
		if not path.is_file() and (generate_markdown or amendment):
			self.plan_node(
				node, requirement_text, path,
				overwrite=self.overwrite,
				temperature=temperature, max_tokens=self.max_tokens,
				dependencies=dependencies,
			)
		if amendment:
			self.amend_node_plan(
				node, requirement_text, path, amendment,
				temperature=temperature,
				max_tokens=self.max_tokens, dependencies=dependencies,
			)
		return StepRunOutput(derived={
			"markdown_path": str(path.resolve()) if path.is_file() else None,
			"output_path": str(path) if path.is_file() else None,
		})

	def _node_type_choice_lines(self) -> list[str]:
		lines = ["- selectable node types:"]
		for reference in workflow_node_references():
			lines.append(
				f"  - {reference.recommended_ext_data_type} -> {reference.meta_node_kind}: {reference.summary}"
			)
		return lines

	def _derive_node_profile(self, node: dict[str, Any]) -> dict[str, Any]:
		ext_data = node.get("ext_data", {}) if isinstance(node, dict) else {}
		reference = resolve_workflow_node_reference(
			meta_node_kind=node.get("meta_node_kind") or node.get("metaNodeKind") if isinstance(node, dict) else None,
			ext_data=ext_data,
		)
		main_utility_signatures = list(
			render_workflow_method_signatures(reference.base_class, reference.main_utility_methods)
		)
		step_output_schema_signatures = list(
			render_workflow_method_signatures(reference.base_class, reference.step_output_schema_methods)
		)
		subclass_implementation_signatures = list(
			render_workflow_method_signatures(reference.base_class, reference.subclass_implementation_methods)
		)
		ext_type = reference.recommended_ext_data_type
		if isinstance(ext_data, dict):
			ext_type = str(ext_data.get("type", "")).strip().lower() or ext_type
		elif isinstance(ext_data, str):
			ext_type = ext_data.strip().lower() or ext_type

		return {
			"extType": ext_type,
			"metaNodeKind": reference.meta_node_kind,
			"capabilityCategory": reference.capability_category,
			"baseClass": reference.meta_node_kind,
			"mainUtilityMethods": list(reference.main_utility_methods),
			"mainUtilitySignatures": main_utility_signatures,
			"stepOutputSchemaMethods": list(reference.step_output_schema_methods),
			"stepOutputSchemaSignatures": step_output_schema_signatures,
			"subclassImplementationMethods": list(reference.subclass_implementation_methods),
			"subclassImplementationSignatures": subclass_implementation_signatures,
			"note": reference.summary,
		}

	def render_node_context(self, node: dict[str, Any]) -> str:
		name = str(node.get("name", "")).strip() or "Node"
		desc = str(node.get("desc", "")).strip()
		depends = node.get("depends", [])
		if not isinstance(depends, list):
			depends = []
		ext_data = node.get("ext_data", {})
		inputs_format = node.get("inputs_format", {})
		if not isinstance(inputs_format, dict):
			inputs_format = {}
		profile = self._derive_node_profile(node)

		ext_desc = ""
		if isinstance(ext_data, dict):
			ext_desc = str(ext_data.get("desc", "")).strip()

		ctx_lines = [
			f"### Node: {name}",
			f"- desc: {desc}",
			f"- depends: {', '.join(depends) if depends else 'none'}",
			f"- meta_node_kind: {profile['metaNodeKind']}",
			f"- ext_data.type: {profile['extType']}",
			f"- ext_data.desc: {ext_desc or 'n/a'}",
		]
		if profile["metaNodeKind"] == "WorkflowStepNode":
			ctx_lines.append(
				f"- inputs_format: {json.dumps(inputs_format, ensure_ascii=False) if inputs_format else 'n/a'}"
			)
		ctx_lines.extend(
			[
				f"- capability category: {profile['capabilityCategory']}",
				f"- recommended base class: {profile['baseClass']}",
				f"- main utility methods: {_method_list_text(profile['mainUtilitySignatures'])}",
				f"- StepRunOutput schema methods: {_method_list_text(profile['stepOutputSchemaSignatures'])}",
				f"- subclass implementation hooks: {_method_list_text(profile['subclassImplementationSignatures'])}",
				f"- note: {profile['note']}",
			]
		)
		ctx_lines.extend(self._node_type_choice_lines())
		return "\n".join(ctx_lines)


__all__ = ["NodePlanElement"]

