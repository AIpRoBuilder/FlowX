import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root
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

	INPUT_REQUIRED = False

	prompt_path: str = "architect/prompts/node_planner_prompt.md"
	default_skills_dirname: str = "skills"
	skills_root_path: str = ""
	node: Optional[dict[str, Any]] = None
	requirement_text: str = ""
	output_path: Optional[str] = None
	index: int = 1
	overwrite: bool = True
	temperature: float = 0.2
	max_tokens: int = MAX_TOKENS
	amendment: str = ""
	generate_markdown: bool = True

	def __post_init__(self) -> None:
		prompt_file = ROOT_DIR / self.prompt_path
		if not prompt_file.exists():
			raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

		base_prompt = prompt_file.read_text(encoding="utf-8")
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
		index: int = 1,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
		dependencies: str = "",
	) -> Path:
		"""Write the brief for one node to the requested path."""
		prompt = self._build_node_prompt(requirement_text, self.render_node_context(node, index), dependencies)
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
		index: int = 1,
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
			f"Node context:\n{self.render_node_context(node, index)}\n\n"
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

	def _dependency_plan_context(self, dependency_results: dict[str, StepRunOutput]) -> str:
		"""Read only direct graph dependencies, not ordering-only workflow edges."""
		depends = self.node.get("depends", []) if isinstance(self.node, dict) else []
		if not isinstance(depends, list):
			return ""
		sections: list[str] = []
		for name in depends:
			if not isinstance(name, str):
				continue
			result = dependency_results.get(f"{name}::audit") or dependency_results.get(name)
			if result is None or not isinstance(result.derived, dict):
				continue
			plan_path = result.derived.get("markdown_path") or result.derived.get("output_path")
			if not isinstance(plan_path, str) or not Path(plan_path).is_file():
				continue
			sections.append(f"### {name}\n{Path(plan_path).read_text(encoding='utf-8')}")
		if not sections:
			return ""
		return "Upstream node plans (dependency results):\n" + "\n\n".join(sections) + "\n\n"

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		del user_input
		if self.node is None or self.output_path is None:
			raise ValueError("Configure node and output_path before running the node plan step.")
		plan_request = session_state.get("node_plan_request")
		if plan_request is not None and not isinstance(plan_request, dict):
			raise TypeError("node_plan_request must be a dictionary")
		plan_request = plan_request or {}
		generate_markdown = plan_request.get("generate_markdown", self.generate_markdown)
		amendment = plan_request.get("amendment", self.amendment)
		if not isinstance(generate_markdown, bool) or not isinstance(amendment, str):
			raise TypeError("node_plan_request requires a boolean generate_markdown and string amendment")
		path = Path(self.output_path)
		dependencies = self._dependency_plan_context(dependency_results)
		if not path.is_file() and (generate_markdown or amendment):
			self.plan_node(
				self.node, self.requirement_text, path,
				index=self.index, overwrite=self.overwrite,
				temperature=self.temperature, max_tokens=self.max_tokens,
				dependencies=dependencies,
			)
		if amendment:
			self.amend_node_plan(
				self.node, self.requirement_text, path, amendment,
				index=self.index, temperature=self.temperature,
				max_tokens=self.max_tokens, dependencies=dependencies,
			)
		return StepRunOutput(derived={
			"markdown_path": str(path.resolve()) if path.is_file() else None,
			"output_path": str(path) if path.is_file() else None,
		})

	def _default_skills_root(self) -> Path:
		if self.skills_root_path:
			configured = Path(self.skills_root_path).expanduser().resolve()
			if configured.is_dir():
				return configured
		root_dir = ROOT_DIR.parent
		direct = root_dir / self.default_skills_dirname
		if direct.is_dir():
			return direct
		parent = root_dir.parent / self.default_skills_dirname
		if parent.is_dir():
			return parent
		return direct

	def _list_available_skills(self) -> list[str]:
		skills_root = self._default_skills_root()
		if not skills_root.is_dir():
			return []
		return sorted(
			child.name
			for child in skills_root.iterdir()
			if child.is_dir() and (child / "skill.md").is_file()
		)

	def _read_skill_markdown(self, skills_root: Path, skill_name: str) -> str:
		if not skill_name:
			return ""
		skill_doc = skills_root / skill_name / "skill.md"
		if not skill_doc.is_file():
			return ""
		return skill_doc.read_text(encoding="utf-8").strip()

	def _extract_skill_description(self, skill_markdown: str) -> str:
		if not skill_markdown.strip():
			return ""
		from flowx_core.tools.file_tools import parse_skill_md

		sections = parse_skill_md(skill_markdown)
		description = sections.get("Description", "").strip()
		if description:
			return " ".join(description.splitlines()).strip()
		for line in skill_markdown.splitlines():
			stripped = line.strip()
			if stripped and not stripped.startswith("#") and not stripped.startswith("`"):
				return stripped
		return ""

	def _skill_descriptions(self) -> dict[str, str]:
		skills_root = self._default_skills_root()
		descriptions: dict[str, str] = {}
		for skill_name in self._list_available_skills():
			skill_markdown = self._read_skill_markdown(skills_root, skill_name)
			description = self._extract_skill_description(skill_markdown)
			if description:
				descriptions[skill_name] = description
		return descriptions

	def _node_type_choice_lines(self) -> list[str]:
		lines = ["- selectable node types:"]
		for reference in workflow_node_references():
			lines.append(
				f"  - {reference.recommended_ext_data_type} -> {reference.meta_node_kind}: {reference.summary}"
			)
		return lines

	def _skill_choice_lines(self) -> list[str]:
		available_skills = self._list_available_skills()
		if not available_skills:
			return ["- available skills: none"]
		descriptions = self._skill_descriptions()
		lines = [
			f"- available skills root: {self._default_skills_root()}",
			"- available skills:",
		]
		for skill_name in available_skills:
			description = descriptions.get(skill_name, "") or "no description found"
			lines.append(f"  - {skill_name}: {description}")
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
		skill_name = ""
		if isinstance(ext_data, dict):
			skill_name = str(ext_data.get("skill_name", "")).strip()

		note = reference.summary
		if reference.meta_node_kind == "WorkflowSkillNode" and skill_name:
			primary_hook = subclass_implementation_signatures[0] if subclass_implementation_signatures else "the selected base-node subclass hook"
			note = (
				f"Skill node wrapping skill '{skill_name}': set SKILL_DIR / SKILL_MD_PATH and implement {primary_hook} around the parsed skill.md guidance."
			)
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
			"note": note,
		}

	def render_node_context(self, node: dict[str, Any], index: int = 1) -> str:
		name = str(node.get("name", "")).strip() or f"Node{index}"
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
		skill_name_val = ""
		if isinstance(ext_data, dict):
			ext_desc = str(ext_data.get("desc", "")).strip()
			skill_name_val = str(ext_data.get("skill_name", "")).strip()

		ctx_lines = [
			f"### Node {index}: {name}",
			f"- desc: {desc}",
			f"- depends: {', '.join(depends) if depends else 'none'}",
			f"- meta_node_kind: {profile['metaNodeKind']}",
			f"- ext_data.type: {profile['extType']}",
			f"- ext_data.desc: {ext_desc or 'n/a'}",
		]
		if profile["metaNodeKind"] in {"WorkflowStepNode", "WorkflowSkillNode"}:
			ctx_lines.append(
				f"- inputs_format: {json.dumps(inputs_format, ensure_ascii=False) if inputs_format else 'n/a'}"
			)
		if skill_name_val:
			ctx_lines.append(f"- ext_data.skill_name: {skill_name_val}")
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
		if profile["metaNodeKind"] == "WorkflowSkillNode":
			ctx_lines.extend(self._skill_choice_lines())
		return "\n".join(ctx_lines)


__all__ = ["NodePlanElement"]

