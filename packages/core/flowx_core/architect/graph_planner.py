# Modified 2026-10-14: reuse this planner in graph generation and consume analyzer output.
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools._node_settings import apply_node_settings
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
		"type": "user_input" | "user_file_input" | "url" | "file" | "db" | "none" | ...,
		"desc": "short description"
	}

	Use `{"type": "user_input", ...}` for nodes that require user input.
	For user_input nodes, include `inputs_format` as an object describing expected
	user input fields and primitive types, e.g.:
	{"email_address": "string", "password": "number"}. 
	Use `{"type": "user_file_input", ...}` for nodes that should be implemented as WorkflowFileNode.
	"""

	STEP_ID = "plan_graph"
	DEPENDENCIES = ["analyze_requirement"]
	INPUT_REQUIRED = False
	__hash__ = object.__hash__

	prompt_path: str = "architect/prompts/graph_planner_prompt.md"
	NONE_EXT_DESC: str = "no need for ext data"

	def __post_init__(self) -> None:
		apply_node_settings(self)
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

	def _graph_plan_contract_prompt(self) -> str:
		operation_reference = resolve_workflow_node_reference(ext_data={"type": "none"})
		lines = [
			"Graph-plan schema requirements:\n",
			"- Top-level JSON shape must be {'nodes': [...]} and the response must contain JSON only.\n",
			"- Every node must include: name, type, desc, meta_node_kind, ext_data, enable.\n",
			"- type must be identical to name.\n",
			"- meta_node_kind must exactly match one value returned by the injected ag_ui_workflow base-node meta_node_kind() catalog.\n",
			"- Use meta_node_kind as the authoritative base-node selection for downstream subclass generation and auditing.\n",
			"- ext_data must be a JSON object with keys: type, desc.\n",
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
				default_ext_type = reference.recommended_ext_data_type
				ext_data = {"type": default_ext_type, "desc": ""}
				if default_ext_type == "none":
					ext_data["desc"] = self.NONE_EXT_DESC
				node["ext_data"] = ext_data

			ext_type = str(ext_data.get("type", "")).strip().lower()
			if reference.meta_node_kind == "WorkflowFileNode":
				ext_type = reference.recommended_ext_data_type
			elif not ext_type:
				ext_type = reference.recommended_ext_data_type
			ext_data["type"] = ext_type
			node["meta_node_kind"] = canonical_meta_node_kind(
				meta_node_kind=node.get("meta_node_kind"),
				ext_data=ext_data,
			)

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

		target_path = Path(output_path).expanduser()
		if target_path.suffix.lower() != ".json":
			target_path = target_path.with_suffix(".json")

		user_prompt = (
			"Generate graph_plan.json from the requirement text below.\n"
			f"{self._graph_plan_contract_prompt()}"
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

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		"""Plan or amend the graph according to the request in session state."""
		del user_input
		request = session_state.get("graph_planner")
		if not isinstance(request, Mapping):
			raise ValueError("session_state['graph_planner'] must be a plan or amend request")

		action_key = f"{type(self).__name__}::action"
		action = session_state.get(action_key)
		if action == "write":
			requirement_text = request.get("requirement_text")
			if requirement_text is None:
				analysis = dependency_results.get("analyze_requirement")
				requirement_path = (
					analysis.derived.get("requirement_md_path") if analysis is not None else None
				) or request.get("requirement_md_path")
				if not isinstance(requirement_path, str) or not requirement_path.strip():
					raise ValueError("write requires requirement_text or requirement_md_path")
				path = Path(requirement_path).expanduser()
				if not path.is_file():
					raise FileNotFoundError(f"Requirement file not found: {path}")
				requirement_text = path.read_text(encoding="utf-8")
			if not isinstance(requirement_text, str) or not requirement_text.strip():
				raise ValueError("write requires non-empty requirement_text")

			output_path = request.get("output_path")
			if not isinstance(output_path, str) or not output_path.strip():
				raise ValueError("write requires a non-empty output_path")
			result_path = self.plan(
				requirement_text,
				output_path,
				overwrite=request.get("overwrite", True),
				temperature=request.get("temperature", 0.2),
				max_tokens=request.get("max_tokens", MAX_TOKENS),
			)
		elif action == "amend":
			graph_json_path = request.get("graph_json_path")
			amendment = request.get("amendment")
			if not isinstance(graph_json_path, str) or not graph_json_path.strip():
				raise ValueError("amend requires a non-empty graph_json_path")
			if not isinstance(amendment, str) or not amendment.strip():
				raise ValueError("amend requires a non-empty amendment")
			result_path = self.amend_file_with_feedback(
				graph_json_path,
				amendment,
				overwrite=request.get("overwrite", True),
				temperature=request.get("temperature", 0.2),
				max_tokens=request.get("max_tokens", MAX_TOKENS),
			)
		else:
			raise ValueError(f"{action_key} must be 'write' or 'amend'")

		return StepRunOutput(
			derived={"graph_plan_json_path": str(Path(result_path).expanduser().resolve())}
		)

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

		target_path = Path(graph_json_path).expanduser()
		if target_path.suffix.lower() != ".json":
			target_path = target_path.with_suffix(".json")

		if not target_path.exists():
			raise FileNotFoundError(f"Graph JSON file not found: {target_path}")

		current_plan = target_path.read_text(encoding="utf-8")

		user_prompt = (
			"Update the existing graph plan JSON using the amendment provided.\n"
			"Preserve the graph schema while applying the amendment.\n"
			f"{self._graph_plan_contract_prompt()}"
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
