import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from pydaograph import CStatus, GElement, GPipeline

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools.workflow_node_reference import (
	render_workflow_method_signatures,
	render_workflow_step_meta_catalog,
	resolve_workflow_node_reference,
	workflow_node_references,
)


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS, append_instruction_block
from flowx_core.tools.runnable_gnode import RunnableGNode, run_gnode_operation


def _method_list_text(method_signatures: list[str]) -> str:
	return ", ".join(method_signatures) if method_signatures else "none"


class _NodePlanElement(GElement):
	def __init__(
		self,
		planner: "NodePlanner",
		node: dict[str, Any],
		index: int,
		target_dir: Path,
		requirement_text: str,
		overwrite: bool,
		temperature: float,
		max_tokens: int,
		outputs: dict[str, Path],
	) -> None:
		super().__init__()
		self.planner = planner
		self.node = node
		self.index = index
		self.target_dir = target_dir
		self.requirement_text = requirement_text
		self.overwrite = overwrite
		self.temperature = temperature
		self.max_tokens = max_tokens
		self.outputs = outputs

	def _build_node_prompt(self, requirement_text: str, node_context: str) -> str:
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
		)

	def run(self) -> CStatus:
		try:
			node_name = str(self.node.get("name", "")).strip() or f"Node{self.index}"
			node_context     = self.planner._node_context(self.node, self.index)
			node_file = self.target_dir / self.planner._node_filename(self.node, self.index)

			user_prompt = self._build_node_prompt(self.requirement_text, node_context)
			written = run_gnode_operation(
				self.planner,
				"code_to_file",
				user_prompt,
				str(node_file),
				overwrite=self.overwrite,
				temperature=self.temperature,
				max_tokens=self.max_tokens,
			)
			self.outputs[node_name] = written
			return CStatus()
		except Exception as exc:
			return CStatus(1001, f"node planning failed for index {self.index}: {exc}")


@dataclass
class NodePlanner(RunnableGNode):
	"""Generate a concise markdown implementation brief for graph nodes.

	The brief is derived from:
	- requirement analysis markdown
	- node desc/dependency/ext_data from graph_plan.json
	- ag_ui_workflow base-node step_meta metadata

	Output format is markdown (.md) that lists suggested tools/functions per node.

	Note: This class is NOT a subclass of Coder. It holds coder configuration and
	creates Coder instances on demand via :meth:`_make_coder`.
	"""

	prompt_path: str = "architect/prompts/node_planner_prompt.md"
	default_skills_dirname: str = "skills"
	skills_root_path: str = ""
	# Coder configuration fields (forwarded to Coder instances)
	provider: str = "openai"
	model: str = "gpt-4.1-mini"
	api_key: Optional[str] = None
	zhipu_thinking: Optional[dict] = None
	deepseek_base_url: str = "https://api.deepseek.com"
	qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
	client: Optional[object] = None
	session_marking_prompt: str = ""

	def __post_init__(self) -> None:
		RunnableGNode.__init__(self)
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
		self.system_prompt = append_instruction_block(
			self.system_prompt,
			self.session_marking_prompt,
		)
		# Build the shared coder used by _NodePlanElement / code_to_file.
		self._coder: Coder = self._make_coder()

	def _make_coder(self) -> Coder:
		"""Create and return a fresh :class:`Coder` configured with this planner's settings."""
		return Coder(
			provider=self.provider,
			model=self.model,
			api_key=self.api_key,
			system_prompt=self.system_prompt,
			zhipu_thinking=self.zhipu_thinking,
			deepseek_base_url=self.deepseek_base_url,
			qwen_base_url=self.qwen_base_url,
			client=self.client,
		)

	def _make_coder_with_system_prompt(self, system_prompt: str) -> Coder:
		"""Create a fresh :class:`Coder` using an override system prompt."""
		return Coder(
			provider=self.provider,
			model=self.model,
			api_key=self.api_key,
			system_prompt=append_instruction_block(system_prompt, self.session_marking_prompt),
			zhipu_thinking=self.zhipu_thinking,
			deepseek_base_url=self.deepseek_base_url,
			qwen_base_url=self.qwen_base_url,
			client=self.client,
		)

	@staticmethod
	def _node_ui_removed_error() -> RuntimeError:
		return RuntimeError("Node UI planning has been removed from flowx_core.")

	def code_to_file(
		self,
		user_prompt: str,
		file_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		"""Delegate to the shared internal :class:`Coder` instance."""
		return self._coder.code_to_file(
			user_prompt,
			file_path,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)

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

	def _build_nodes_context(self, graph_plan: dict[str, Any]) -> str:
		nodes = graph_plan.get("nodes", []) if isinstance(graph_plan, dict) else []
		if not isinstance(nodes, list) or not nodes:
			return "(No nodes found in graph plan)"

		lines: list[str] = []
		for idx, node in enumerate(nodes, start=1):
			if not isinstance(node, dict):
				continue

			name = str(node.get("name", "")).strip() or f"Node{idx}"
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

			node_lines = [
				f"### Node {idx}: {name}",
				f"- desc: {desc}",
				f"- depends: {', '.join(depends) if depends else 'none'}",
				f"- meta_node_kind: {profile['metaNodeKind']}",
				f"- ext_data.type: {profile['extType']}",
				f"- ext_data.desc: {ext_desc or 'n/a'}",
			]
			if profile["metaNodeKind"] in {"WorkflowStepNode", "WorkflowSkillNode"}:
				node_lines.append(
					f"- inputs_format: {json.dumps(inputs_format, ensure_ascii=False) if inputs_format else 'n/a'}"
				)
			if skill_name_val:
				node_lines.append(f"- ext_data.skill_name: {skill_name_val}")
			node_lines.extend(
				[
					f"- capability category: {profile['capabilityCategory']}",
					f"- recommended base class: {profile['baseClass']}",
					f"- main utility methods: {_method_list_text(profile['mainUtilitySignatures'])}",
					f"- StepRunOutput schema methods: {_method_list_text(profile['stepOutputSchemaSignatures'])}",
					f"- subclass implementation hooks: {_method_list_text(profile['subclassImplementationSignatures'])}",
					f"- note: {profile['note']}",
					"",
				]
			)
			lines.extend(node_lines)

		return "\n".join(lines).strip()

	def _extract_nodes(self, graph_plan: dict[str, Any]) -> list[dict[str, Any]]:
		nodes = graph_plan.get("nodes", []) if isinstance(graph_plan, dict) else []
		if not isinstance(nodes, list):
			return []
		return [node for node in nodes if isinstance(node, dict)]

	def _node_context(self, node: dict[str, Any], index: int) -> str:
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

	def _node_filename(self, node: dict[str, Any], index: int) -> str:
		name = str(node.get("name", "")).strip() or f"Node{index}"
		return f"{name}.md"

	def plan_each_from_files(
		self,
		requirement_md_path: str,
		graph_plan_json_path: str,
		output_dir: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.05,
		max_tokens: int = MAX_TOKENS,
	) -> list[Path]:
		"""Generate one brief markdown file per node, named `<node_name>.md`."""

		requirement_path = Path(requirement_md_path)
		if not requirement_path.exists():
			raise FileNotFoundError(f"Requirement file not found: {requirement_path}")

		graph_path = Path(graph_plan_json_path)
		if not graph_path.exists():
			raise FileNotFoundError(f"Graph plan file not found: {graph_path}")

		requirement_text = requirement_path.read_text(encoding="utf-8")
		graph_plan_text = graph_path.read_text(encoding="utf-8")

		return self.plan_each(
			requirement_text=requirement_text,
			graph_plan_text=graph_plan_text,
			output_dir=output_dir,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)

	def plan_each(
		self,
		requirement_text: str,
		graph_plan_text: str,
		output_dir: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> list[Path]:
		"""Generate one brief node summary markdown for each node into `output_dir`.

		Each file is intentionally high-level and includes only:
		- what the node will achieve
		- core function names with short purposes
		"""

		try:
			graph_plan: dict[str, Any] = json.loads(graph_plan_text)
		except json.JSONDecodeError as exc:
			raise ValueError(f"Invalid graph plan JSON: {exc}") from exc

		nodes = self._extract_nodes(graph_plan)
		if not nodes:
			return []

		target_dir = Path(output_dir)
		target_dir.mkdir(parents=True, exist_ok=True)

		pipeline = GPipeline()
		node_entries: list[tuple[int, dict[str, Any], str]] = []
		elements: dict[str, _NodePlanElement] = {}
		node_outputs: dict[str, Path] = {}

		for index, node in enumerate(nodes, start=1):
			node_name = str(node.get("name", "")).strip() or f"Node{index}"
			node_entries.append((index, node, node_name))
			elements[node_name] = _NodePlanElement(
				planner=self,
				node=node,
				index=index,
				target_dir=target_dir,
				requirement_text=requirement_text,
				overwrite=overwrite,
				temperature=temperature,
				max_tokens=max_tokens,
				outputs=node_outputs,
			)

		for index, node, node_name in node_entries:
			depends = node.get("depends", [])
			dep_names = depends if isinstance(depends, list) else []
			dep_elements = {elements[dep_name] for dep_name in dep_names if dep_name in elements}
			status = pipeline.registerGElement(elements[node_name], dep_elements, node_name, 1)
			if status.isErr():
				raise RuntimeError(f"registerGElement failed for {node_name}: {status.getInfo()}")

		process_status = pipeline.process()
		if process_status.isErr():
			raise RuntimeError(f"plan_each pipeline.process failed: {process_status.getInfo()}")

		output_paths: list[Path] = []
		for index, _node, node_name in node_entries:
			if node_name in node_outputs:
				output_paths.append(node_outputs[node_name])
			else:
				expected = target_dir / self._node_filename(_node, index)
				if expected.exists():
					output_paths.append(expected)

		return output_paths

	def _build_amend_graph_node_prompt(
		self,
		user_prompt: str,
		requirement_text: str,
		node_context: str,
		existing_node_json: str,
	) -> str:
		return (
			"Rewrite ONLY the target node JSON object from an existing graph plan.\n"
			"Return exactly one valid JSON object for the node. No markdown fences, no commentary.\n"
			"Preserve the node name unless the amendment explicitly requires a rename.\n"
			"Node schema requirements:\n"
			"- Required fields: name, type, desc, meta_node_kind, enable, depends, ext_data\n"
			"- ext_data must be a JSON object with keys: type, desc, and skill_name when type='skill'\n"
			"- Allowed meta_node_kind choices are listed in the node context below\n"
			"- Include inputs_format only when the selected meta_node_kind allows it\n"
			"- Do not include inputs_format for other meta_node_kind values\n"
			"- For skill nodes, ext_data.skill_name must match one listed available skill exactly\n\n"
			"=== Requirement Analysis ===\n"
			f"{requirement_text}\n\n"
			"=== Target Node Context ===\n"
			f"{node_context}\n\n"
			"=== Existing Node JSON ===\n"
			f"{existing_node_json}\n\n"
			"=== Amendment Instructions ===\n"
			f"{user_prompt}\n"
		)

	def _write_amended_graph_node(
		self,
		node_name: str,
		user_prompt: str,
		requirement_text: str,
		graph_plan_text: str,
		graph_output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> tuple[Path, dict[str, Any], int]:
		try:
			graph_plan: dict[str, Any] = json.loads(graph_plan_text)
		except json.JSONDecodeError as exc:
			raise ValueError(f"Invalid graph plan JSON: {exc}") from exc

		nodes = self._extract_nodes(graph_plan)
		matched_node: dict[str, Any] | None = None
		matched_index = 1
		for idx, node in enumerate(nodes, start=1):
			name = str(node.get("name", "")).strip() or f"Node{idx}"
			if name == node_name:
				matched_node = node
				matched_index = idx
				break

		if matched_node is None:
			raise ValueError(
				f"Node '{node_name}' not found in graph plan. "
				f"Available nodes: {[str(n.get('name', '')).strip() for n in nodes]}"
			)

		output_target = Path(graph_output_path)
		if output_target.exists() and not overwrite:
			raise FileExistsError(f"Output file exists and overwrite=False: {output_target}")

		node_context = self._node_context(matched_node, matched_index)
		prompt = self._build_amend_graph_node_prompt(
			user_prompt=user_prompt,
			requirement_text=requirement_text,
			node_context=node_context,
			existing_node_json=json.dumps(matched_node, ensure_ascii=False, indent=2),
		)
		amend_coder = self._make_coder_with_system_prompt(
			"You are a precise workflow graph planner. Return only valid JSON for the requested single node object."
		)
		updated_node_raw = amend_coder.generate_code(
			prompt,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		try:
			updated_node = json.loads(updated_node_raw)
		except json.JSONDecodeError as exc:
			raise ValueError(f"Invalid amended node JSON returned by model: {exc}") from exc
		if not isinstance(updated_node, dict):
			raise ValueError("Amended node output must be a JSON object.")

		resolved_name = str(updated_node.get("name", "")).strip() or node_name
		if resolved_name != node_name:
			updated_node["name"] = node_name

		graph_nodes = graph_plan.get("nodes", []) if isinstance(graph_plan, dict) else []
		if not isinstance(graph_nodes, list):
			raise ValueError("Graph plan must contain a top-level 'nodes' list.")
		graph_nodes[matched_index - 1] = updated_node

		output_target.parent.mkdir(parents=True, exist_ok=True)
		output_target.write_text(
			json.dumps(graph_plan, ensure_ascii=False, indent=2),
			encoding="utf-8",
		)

		from flowx_core.architect.graph_planner import GraphPlanner

		normalizer = GraphPlanner(
			provider=self.provider,
			model=self.model,
			api_key=self.api_key,
			skills_root_path=self.skills_root_path,
			zhipu_thinking=self.zhipu_thinking,
			deepseek_base_url=self.deepseek_base_url,
			qwen_base_url=self.qwen_base_url,
			client=self.client,
			session_marking_prompt=self.session_marking_prompt,
		)
		normalizer._normalize_ext_data_in_file(output_target)

		normalized_plan = json.loads(output_target.read_text(encoding="utf-8"))
		normalized_nodes = self._extract_nodes(normalized_plan)
		normalized_node = normalized_nodes[matched_index - 1]
		return output_target, normalized_node, matched_index

	def _regenerate_single_node_plan(
		self,
		node: dict[str, Any],
		index: int,
		requirement_text: str,
		output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		node_name = str(node.get("name", "")).strip() or f"Node{index}"
		output_target = Path(output_path)
		outputs: dict[str, Path] = {}
		element = _NodePlanElement(
			planner=self,
			node=node,
			index=index,
			target_dir=output_target.parent,
			requirement_text=requirement_text,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
			outputs=outputs,
		)
		status = element.run()
		if status.isErr():
			raise RuntimeError(f"node plan regeneration failed for '{node_name}': {status.getInfo()}")

		written = outputs.get(node_name)
		if written is None:
			written = element.target_dir / self._node_filename(node, index)
		if not written.exists():
			raise RuntimeError(
				f"node plan regeneration did not produce expected output file for '{node_name}': {written}"
			)

		if written.resolve() == output_target.resolve():
			return written

		if output_target.exists() and not overwrite:
			raise FileExistsError(f"Output file exists and overwrite=False: {output_target}")

		output_target.parent.mkdir(parents=True, exist_ok=True)
		output_target.write_text(written.read_text(encoding="utf-8"), encoding="utf-8")
		return output_target

	def amend_graph_node(
		self,
		node_name: str,
		user_prompt: str,
		requirement_text: str,
		graph_plan_text: str,
		graph_output_path: str,
		node_output_path: str,
		node_ui_output_path: str | None = None,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> tuple[Path, Path]:
		"""Amend one node inside graph_plan JSON, then regenerate its markdown plan."""
		del node_ui_output_path
		graph_path, normalized_node, node_index = self._write_amended_graph_node(
			node_name=node_name,
			user_prompt=user_prompt,
			requirement_text=requirement_text,
			graph_plan_text=graph_plan_text,
			graph_output_path=graph_output_path,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		node_plan_path = self._regenerate_single_node_plan(
			node=normalized_node,
			index=node_index,
			requirement_text=requirement_text,
			output_path=node_output_path,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		return graph_path, node_plan_path

	def amend_graph_node_from_files(
		self,
		node_name: str,
		user_prompt: str,
		requirement_md_path: str,
		graph_plan_json_path: str,
		node_output_path: str,
		node_ui_output_path: str | None = None,
		graph_output_path: str | None = None,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> tuple[Path, Path]:
		"""File-based wrapper for :meth:`amend_graph_node`."""
		requirement_path = Path(requirement_md_path)
		if not requirement_path.exists():
			raise FileNotFoundError(f"Requirement file not found: {requirement_path}")

		graph_path = Path(graph_plan_json_path)
		if not graph_path.exists():
			raise FileNotFoundError(f"Graph plan file not found: {graph_path}")

		requirement_text = requirement_path.read_text(encoding="utf-8")
		graph_plan_text = graph_path.read_text(encoding="utf-8")
		dest_graph_path = graph_output_path if graph_output_path is not None else str(graph_path)

		return self.amend_graph_node(
			node_name=node_name,
			user_prompt=user_prompt,
			requirement_text=requirement_text,
			graph_plan_text=graph_plan_text,
			graph_output_path=dest_graph_path,
			node_output_path=node_output_path,
			node_ui_output_path=node_ui_output_path,
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)

	def amend_node_ui(
		self,
		node_name: str,
		user_prompt: str,
		old_html: str,
		requirement_text: str,
		graph_plan_text: str,
		output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		del node_name, user_prompt, old_html, requirement_text, graph_plan_text, output_path, overwrite, temperature, max_tokens
		raise self._node_ui_removed_error()

	def amend_node_ui_from_files(
		self,
		node_name: str,
		user_prompt: str,
		existing_html_path: str,
		requirement_md_path: str,
		graph_plan_json_path: str,
		output_path: str | None = None,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> Path:
		del node_name, user_prompt, existing_html_path, requirement_md_path, graph_plan_json_path, output_path, overwrite, temperature, max_tokens
		raise self._node_ui_removed_error()

	def plan_each_ui_from_files(
		self,
		requirement_md_path: str,
		graph_plan_json_path: str,
		output_dir: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> list[Path]:
		del requirement_md_path, graph_plan_json_path, output_dir, overwrite, temperature, max_tokens
		raise self._node_ui_removed_error()

	def plan_each_ui(
		self,
		requirement_text: str,
		graph_plan_text: str,
		output_dir: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.05,
		max_tokens: int = MAX_TOKENS,
	) -> list[Path]:
		del requirement_text, graph_plan_text, output_dir, overwrite, temperature, max_tokens
		raise self._node_ui_removed_error()