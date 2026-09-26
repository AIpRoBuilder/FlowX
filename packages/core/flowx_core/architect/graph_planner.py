import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools.workflow_node_reference import (
	canonical_meta_node_kind,
	render_workflow_step_meta_catalog,
	resolve_workflow_node_reference,
	workflow_node_references,
)


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS


@dataclass
class GraphPlanner(Coder):
	"""Generate a JSON graph plan from a requirements analysis markdown.

	Nodes in the plan should be objects with keys: `name`, `type`, `desc`,
	`depends`, `ext_data`, and optional `inputs_format`.

	`ext_data` should be a JSON object for every node with shape:
	{
		"type": "user_input" | "user_file_input" | "url" | "file" | "db" | "skill" | "none" | ...,
		"desc": "short description",
		"skill_name": "optional skill directory name (required when type=skill)"
	}

	Use `{"type": "user_input", ...}` for nodes that require user input.
	For user_input and skill nodes, include `inputs_format` as an object describing expected
	user input fields and primitive types, e.g.:
	{"email_address": "string", "password": "number"}. 
	Use `{"type": "user_file_input", ...}` for nodes that should be implemented as WorkflowFileNode.
	Use `{"type": "skill", "skill_name": "<skill>", ...}` for nodes that should be implemented as WorkflowSkillNode.
	"""

	prompt_path: str = "architect/prompts/graph_planner_prompt.md"
	default_skills_dirname: str = "skills"
	skills_root_path: str = ""
	NONE_EXT_DESC: str = "no need for ext data"
	SKILL_EXT_DESC: str = "skill node"

	def __post_init__(self) -> None:
		prompt_file = ROOT_DIR / self.prompt_path
		if not prompt_file.exists():
			raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

		base_prompt = prompt_file.read_text(encoding="utf-8")
		workflow_step_meta_catalog_text = render_workflow_step_meta_catalog()
		self.system_prompt = (
			f"{base_prompt}\n\n"
			"## ag_ui_workflow Base Step Metas (Authoritative)\n"
			"When selecting workflow node structure, node kind and required capabilities,\n"
			"strictly follow the injected ag_ui_workflow base-node step_meta()/meta_node_kind() catalog below.\n\n"
			f"{workflow_step_meta_catalog_text}\n"
		)
		super().__post_init__()

	# ------------------------------------------------------------------
	# Skill helpers
	# ------------------------------------------------------------------

	def _default_skills_root(self) -> Path:
		if self.skills_root_path:
			configured = Path(self.skills_root_path).expanduser().resolve()
			if configured.is_dir():
				return configured
		root_dir = ROOT_DIR.parent
		direct = root_dir / self.default_skills_dirname
		if direct.is_dir():
			return direct
		# also look one level up
		parent = root_dir.parent / self.default_skills_dirname
		if parent.is_dir():
			return parent
		# fall back to sibling of the core package root
		pkg_sibling = ROOT_DIR.parent / self.default_skills_dirname
		if pkg_sibling.is_dir():
			return pkg_sibling
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
		"""Return the text under the ## Description section of a skill.md."""
		if not skill_markdown.strip():
			return ""
		from flowx_core.tools.file_tools import parse_skill_md
		sections = parse_skill_md(skill_markdown)
		desc = sections.get("Description", "").strip()
		if desc:
			return " ".join(desc.splitlines()).strip()
		# fall back to first non-empty, non-heading line
		for line in skill_markdown.splitlines():
			stripped = line.strip()
			if stripped and not stripped.startswith("#") and not stripped.startswith("`"):
				return stripped
		return ""

	def _skill_descriptions(self, skills_root: Path, available_skills: list[str]) -> dict[str, str]:
		descriptions: dict[str, str] = {}
		for skill_name in available_skills:
			skill_markdown = self._read_skill_markdown(skills_root, skill_name)
			description = self._extract_skill_description(skill_markdown)
			if description:
				descriptions[skill_name] = description
		return descriptions

	def _normalize_skill_key(self, value: str) -> str:
		clean = value.strip().lower()
		for sep in ("_", "-", " "):
			clean = clean.replace(sep, "")
		return clean

	def _resolve_skill_name(self, node: dict[str, Any], available_skills: list[str]) -> str:
		if not available_skills:
			return ""

		ext_data = node.get("ext_data", {})
		skill_name = ""
		if isinstance(ext_data, dict):
			skill_name = str(ext_data.get("skill_name", "")).strip()

		by_key = {self._normalize_skill_key(name): name for name in available_skills}

		if skill_name:
			matched = by_key.get(self._normalize_skill_key(skill_name))
			if matched:
				return matched

		search_texts: list[str] = []
		for key in ("name", "type", "desc"):
			val = node.get(key, "")
			if isinstance(val, str) and val.strip():
				search_texts.append(val)
		if isinstance(ext_data, dict):
			ext_desc = ext_data.get("desc", "")
			if isinstance(ext_desc, str) and ext_desc.strip():
				search_texts.append(ext_desc)

		for text in search_texts:
			norm_text = self._normalize_skill_key(text)
			for key, canonical in by_key.items():
				if key and key in norm_text:
					return canonical

		if len(available_skills) == 1:
			return available_skills[0]

		return ""

	def _skill_bootstrap_desc(self, skill_name: str, skill_descriptions: dict[str, str]) -> str:
		description = skill_descriptions.get(skill_name, "").strip()
		if not description:
			return self.SKILL_EXT_DESC
		return f"skill node: {description}"

	def _build_skill_context_prompt(self) -> str:
		skills_root = self._default_skills_root()
		available_skills = self._list_available_skills()
		skill_descriptions = self._skill_descriptions(skills_root, available_skills)
		skills_text = ", ".join(available_skills) if available_skills else "none"
		skill_lines = [
			"Skill planning context:\n"
			f"- default_skills_root: {skills_root}\n"
			f"- available_skills_name: {skills_text}\n"
			"- If choosing WorkflowSkillNode semantics, set ext_data.type='skill' and ext_data.skill_name to one exact available_skills_name entry.\n"
		]
		if skill_descriptions:
			skill_lines.append("- skill_descriptions_from_skill_md:\n")
			for skill_name in available_skills:
				description = skill_descriptions.get(skill_name, "")
				if not description:
					continue
				skill_lines.append(f"  - {skill_name}: {description}\n")
			skill_lines.append(
				"- For skill-type nodes, choose skill_name by matching requirement semantics to skill_descriptions_from_skill_md, and reflect that purpose in node desc/ext_data.desc.\n"
			)
		return "".join(skill_lines)

	def _graph_plan_contract_prompt(self) -> str:
		operation_reference = resolve_workflow_node_reference(ext_data={"type": "none"})
		lines = [
			"Graph-plan schema requirements:\n",
			"- Top-level JSON shape must be {'nodes': [...]} and the response must contain JSON only.\n",
			"- Every node must include: name, type, desc, meta_node_kind, ext_data, enable.\n",
			"- type must be identical to name.\n",
			"- meta_node_kind must exactly match one value returned by the injected ag_ui_workflow base-node meta_node_kind() catalog.\n",
			"- Use meta_node_kind as the authoritative base-node selection for downstream subclass generation and auditing.\n",
			"- ext_data must be a JSON object with keys: type, desc, plus skill_name when the selected base node is a skill node.\n",
			"- Use node-level loop to represent repeated execution; default loop=1.\n",
			"- Include inputs_format only when the selected base node's step_meta contract supports explicit input fields.\n",
			"- Do not invent node categories outside the injected ag_ui_workflow base-node catalog.\n",
		]
		for reference in workflow_node_references():
			if reference.meta_node_kind == operation_reference.meta_node_kind:
				lines.append(
					f"- When meta_node_kind='{reference.meta_node_kind}', ext_data.type may be 'none' or a domain source such as 'url', 'file', or 'db'; if it is 'none', ext_data.desc must be '{self.NONE_EXT_DESC}'.\n"
				)
				continue
			line = (
				f"- When meta_node_kind='{reference.meta_node_kind}', ext_data.type must be '{reference.recommended_ext_data_type}'."
			)
			if reference.recommended_ext_data_type == "skill":
				line += " ext_data.skill_name must be an exact available skill directory name."
			lines.append(f"{line}\n")
		lines.extend(
			[
				"- Example inputs_format shape: {'email_address':'string','password':'number'}.\n",
				"- Example iterative node: {'name':'UserInput','type':'UserInput','desc':'接收用户输入的目标用户画像与教学大纲文本','meta_node_kind':'WorkflowStepNode','loop':2,'ext_data':{'type':'user_input','desc':'输入目标用户画像和教学大纲文本'},'inputs_format':{'target_profile':'string','teaching_outline':'string'},'enable':true}.\n",
			]
		)
		return "".join(lines)

	def _normalize_ext_data_in_file(self, graph_json_path: Path) -> None:
		# Normalize ext_data shape and enforce type=none description rules.

		payload = json.loads(graph_json_path.read_text(encoding="utf-8"))
		nodes = payload.get("nodes", [])
		if not isinstance(nodes, list):
			return

		available_skills = self._list_available_skills()
		skills_root = self._default_skills_root()
		skill_descriptions = self._skill_descriptions(skills_root, available_skills)

		for node in nodes:
			if not isinstance(node, dict):
				continue

			node.pop("show_frontend", None)
			node.pop("services", None)
			legacy_meta_node_kind = node.pop("metaNodeKind", None)

			loop_value = node.get("loop", 1)
			try:
				loop_int = int(loop_value)
			except (TypeError, ValueError):
				loop_int = 1
			node["loop"] = max(1, loop_int)

			meta_node_kind = str(
				node.get("meta_node_kind") or legacy_meta_node_kind or ""
			).strip() or None
			reference = resolve_workflow_node_reference(
				meta_node_kind=meta_node_kind,
				ext_data=node.get("ext_data"),
			)
			node["meta_node_kind"] = reference.meta_node_kind

			ext_data = node.get("ext_data")
			if not isinstance(ext_data, dict):
				default_ext_type = (
					reference.recommended_ext_data_type
					if reference.meta_node_kind != "WorkflowOperationNode"
					else "none"
				)
				ext_data = {"type": default_ext_type, "desc": ""}
				if default_ext_type == "none":
					ext_data["desc"] = self.NONE_EXT_DESC
				if default_ext_type == "skill":
					ext_data["skill_name"] = ""
				node["ext_data"] = ext_data

			ext_type = str(ext_data.get("type", "")).strip().lower()
			if reference.meta_node_kind != "WorkflowOperationNode":
				ext_type = reference.recommended_ext_data_type
			elif not ext_type:
				ext_type = "none"
			ext_data["type"] = ext_type
			node["meta_node_kind"] = canonical_meta_node_kind(
				meta_node_kind=node.get("meta_node_kind"),
				ext_data=ext_data,
			)

			# ---- skill node normalisation ----
			skill_name = str(ext_data.get("skill_name", "")).strip()
			if node["meta_node_kind"] == "WorkflowSkillNode" or ext_type == "skill" or skill_name:
				ext_data["type"] = "skill"
				resolved_skill = self._resolve_skill_name(node, available_skills)
				ext_data["skill_name"] = resolved_skill
				desc = str(ext_data.get("desc", "")).strip()
				ext_data["desc"] = desc or self._skill_bootstrap_desc(
					resolved_skill,
					skill_descriptions,
				)
				# normalize inputs_format for skill nodes
				raw_inputs_format = node.get("inputs_format", {})
				normalized_inputs_format: dict[str, str] = {}
				if isinstance(raw_inputs_format, dict):
					for key, value in raw_inputs_format.items():
						input_key = str(key).strip()
						input_type = str(value).strip().lower()
						if input_key and input_type:
							normalized_inputs_format[input_key] = input_type
				if normalized_inputs_format:
					node["inputs_format"] = normalized_inputs_format
				else:
					node.pop("inputs_format", None)
				node["meta_node_kind"] = "WorkflowSkillNode"
				continue

			if node["meta_node_kind"] == "WorkflowStepNode":
				raw_inputs_format = node.get("inputs_format", {})
				normalized_inputs_format: dict[str, str] = {}
				if isinstance(raw_inputs_format, dict):
					for key, value in raw_inputs_format.items():
						input_key = str(key).strip()
						input_type = str(value).strip().lower()
						if input_key and input_type:
							normalized_inputs_format[input_key] = input_type
				if normalized_inputs_format:
					node["inputs_format"] = normalized_inputs_format
				else:
					node.pop("inputs_format", None)
			else:
				node.pop("inputs_format", None)

			if ext_type == "none":
				ext_data["desc"] = self.NONE_EXT_DESC
			else:
				desc = str(ext_data.get("desc", "")).strip()
				ext_data["desc"] = desc
			ext_data.pop("service_name", None)
			if node["meta_node_kind"] != "WorkflowSkillNode":
				ext_data.pop("skill_name", None)
			elif "skill_name" in ext_data and ext_data["skill_name"] is None:
				ext_data["skill_name"] = ""

		graph_json_path.write_text(
			json.dumps(payload, ensure_ascii=False, indent=2),
			encoding="utf-8",
		)

	def _safe_mermaid_id(self, node_name: str, used_ids: set[str]) -> str:
		base_chars: list[str] = []
		for ch in str(node_name):
			if ch.isalnum() or ch == "_":
				base_chars.append(ch)
			else:
				base_chars.append("_")
		base = "".join(base_chars).strip("_")
		if not base:
			base = "node"
		if not (base[0].isalpha() or base[0] == "_"):
			base = f"n_{base}"

		candidate = base
		suffix = 2
		while candidate in used_ids:
			candidate = f"{base}_{suffix}"
			suffix += 1
		used_ids.add(candidate)
		return candidate

	def _to_mermaid_text(self, graph_json_path: Path) -> str:
		payload = json.loads(graph_json_path.read_text(encoding="utf-8"))
		nodes = payload.get("nodes", [])
		if not isinstance(nodes, list):
			nodes = []

		used_ids: set[str] = set()
		name_to_id: dict[str, str] = {}
		for node in nodes:
			if not isinstance(node, dict):
				continue
			name = str(node.get("name", "")).strip()
			if not name or name in name_to_id:
				continue
			name_to_id[name] = self._safe_mermaid_id(name, used_ids)

		lines: list[str] = ["flowchart TD"]
		for node in nodes:
			if not isinstance(node, dict):
				continue
			name = str(node.get("name", "")).strip()
			if not name or name not in name_to_id:
				continue
			node_id = name_to_id[name]
			label = name.replace('"', "\\\"")
			lines.append(f'    {node_id}["{label}"]')

		edges_seen: set[tuple[str, str]] = set()
		for node in nodes:
			if not isinstance(node, dict):
				continue
			name = str(node.get("name", "")).strip()
			if not name or name not in name_to_id:
				continue
			target_id = name_to_id[name]
			depends = node.get("depends", [])
			if isinstance(depends, (str, bytes)):
				depends = [depends]
			if not isinstance(depends, list):
				continue
			for dep in depends:
				dep_name = str(dep).strip()
				if not dep_name:
					continue
				dep_id = name_to_id.get(dep_name)
				if dep_id is None:
					dep_id = self._safe_mermaid_id(dep_name, used_ids)
					name_to_id[dep_name] = dep_id
					label = dep_name.replace('"', "\\\"")
					lines.append(f'    {dep_id}["{label}"]')
				edge = (dep_id, target_id)
				if edge in edges_seen:
					continue
				edges_seen.add(edge)
				lines.append(f"    {dep_id} --> {target_id}")

		return "\n".join(lines).strip() + "\n"

	def _write_mermaid_from_graph_json(self, graph_json_path: Path) -> Path:
		mmd_path = graph_json_path.with_suffix(".mmd")
		mmd_text = self._to_mermaid_text(graph_json_path)
		mmd_path.write_text(mmd_text, encoding="utf-8")
		return mmd_path

	def plan_from_file(
		self,
		requirement_md_path: str,
		output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		"""Read requirement_analysis.md and write a JSON graph plan."""

		requirement_path = Path(requirement_md_path)
		if not requirement_path.exists():
			raise FileNotFoundError(f"Requirement file not found: {requirement_path}")

		requirement_text = requirement_path.read_text(encoding="utf-8")
		return self.plan(
			requirement_text,
			output_path,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)

	def plan(
		self,
		requirement_text: str,
		output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.05,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		"""Call the LLM and persist the graph plan as JSON."""

		target_path = Path(output_path)
		if target_path.suffix.lower() != ".json":
			target_path = target_path.with_suffix(".json")

		user_prompt = (
			"Generate graph_plan.json from the requirement text below.\n"
			f"{self._graph_plan_contract_prompt()}"
			f"{self._build_skill_context_prompt()}"
			"Return only valid JSON.\n\n"
			"Requirement text:\n"
			f"{requirement_text}"
		)

		result_path = self.code_to_file(
			user_prompt,
			str(target_path),
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		graph_path = Path(result_path)
		self._normalize_ext_data_in_file(graph_path)
		return result_path

	def amend_file_with_feedback(
		self,
		graph_json_path: str,
		amendment: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		"""Amend an existing graph JSON plan using feedback."""

		target_path = Path(graph_json_path)
		if target_path.suffix.lower() != ".json":
			target_path = target_path.with_suffix(".json")

		if not target_path.exists():
			raise FileNotFoundError(f"Graph JSON file not found: {target_path}")

		current_plan = target_path.read_text(encoding="utf-8")

		user_prompt = (
			"Update the existing graph plan JSON using the amendment provided.\n"
			"Preserve the graph schema while applying the amendment.\n"
			f"{self._graph_plan_contract_prompt()}"
			f"{self._build_skill_context_prompt()}"
			"Return only valid JSON without code fences or commentary.\n\n"
			"Existing graph plan:\n"
			f"{current_plan}\n\n"
			"Amendment / feedback to apply:\n"
			f"{amendment}\n"
		)

		result_path = self.code_to_file(
			user_prompt,
			str(target_path),
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		graph_path = Path(result_path)
		self._normalize_ext_data_in_file(graph_path)
		return result_path
