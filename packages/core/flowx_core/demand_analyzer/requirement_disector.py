from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root
from flowx_core.tools.workflow_node_reference import render_workflow_step_meta_catalog


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS


@dataclass
class RequirementAnalysisResult:
	"""Artifact produced from requirement analysis."""

	output_path: Path


@dataclass
class RequirementDisector(Coder):
	"""Generate requirements analysis markdown from a demand description."""

	prompt_path: str = "demand_analyzer/prompts/requirement_disector_prompt.md"

	def __post_init__(self) -> None:
		prompt_file = ROOT_DIR / self.prompt_path
		if not prompt_file.exists():
			raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

		self.system_prompt = (
			f"{prompt_file.read_text(encoding='utf-8')}\n\n"
			"## ag_ui_workflow Base Step Metas (Authoritative)\n"
			"节点设计时必须使用下面注入的 ag_ui_workflow 基类 step_meta()/meta_node_kind() 结果作为唯一权威来源。\n\n"
			f"{render_workflow_step_meta_catalog()}\n"
		)
		super().__post_init__()

	def process_input(
		self,
		user_input: str,
		dependency_results: dict[str, StepRunOutput],
		session_state: dict[str, Any],
	) -> StepRunOutput:
		"""Write or amend requirement analysis from a session-state request."""
		del dependency_results
		request = session_state.get("requirement_disector")
		if not isinstance(request, Mapping):
			raise ValueError("session_state['requirement_disector'] must be a write or amend request")

		action_key = f"{type(self).__name__}::action"
		action = session_state.get(action_key)
		if action not in ("write", "amend"):
			raise ValueError(f"{action_key} must be 'write' or 'amend'")

		output_path = request.get("output_path")
		if not isinstance(output_path, str) or not output_path.strip():
			raise ValueError("requirement_disector output_path must be a non-empty string")
		options = {
			"overwrite": request.get("overwrite", True),
			"temperature": request.get("temperature", 0.2),
			"max_tokens": request.get("max_tokens", MAX_TOKENS),
		}
		if action == "write":
			requirement_text = request.get("requirement_text", user_input)
			if not isinstance(requirement_text, str) or not requirement_text.strip():
				raise ValueError("write requires non-empty requirement_text or user_input")
			result = self.analyze(requirement_text, output_path, **options)
		else:
			amendment = request.get("amendment")
			if not isinstance(amendment, str) or not amendment.strip():
				raise ValueError("amend requires a non-empty amendment")
			result = self.amend_analysis(output_path, amendment, **options)
		return StepRunOutput(derived={"requirement_md_path": str(result.output_path)})

	def analyze(
		self,
		user_prompt: str,
		output_path: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> RequirementAnalysisResult:
		"""Call the LLM and persist the requirements analysis."""

		target_path = Path(output_path)
		if target_path.suffix.lower() != ".md":
			target_path = target_path.with_suffix(".md")

		written_path = self.code_to_file(
			user_prompt,
			str(target_path),
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		return RequirementAnalysisResult(output_path=written_path)

	def amend_analysis(
		self,
		output_path: str,
		amendment: str,
		*,
		overwrite: bool = True,
		temperature: float = 0.2,
		max_tokens: int = MAX_TOKENS,
	) -> RequirementAnalysisResult:
		"""Update an existing requirement analysis using feedback."""
		target_path = Path(output_path)
		if target_path.suffix.lower() != ".md":
			target_path = target_path.with_suffix(".md")
		if not target_path.is_file():
			raise FileNotFoundError(f"Requirement analysis not found: {target_path}")

		prompt = (
			"Revise the existing requirement analysis according to the feedback. "
			"Preserve valid requirements and return only the complete updated Markdown.\n\n"
			f"Existing analysis:\n{target_path.read_text(encoding='utf-8')}\n\n"
			f"Feedback:\n{amendment}\n"
		)
		written_path = self.code_to_file(
			prompt,
			str(target_path),
			overwrite=overwrite,
			temperature=temperature,
			max_tokens=max_tokens,
		)
		return RequirementAnalysisResult(output_path=written_path)
