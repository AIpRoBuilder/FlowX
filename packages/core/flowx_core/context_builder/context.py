"""Context data model and base builder for associating files."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class Context:
	"""Represents relevance between the active file and a context file."""

	current_file_location: str
	current_file_name: str
	context_file_location: str
	context_file_name: str
	context_file_description: str
	context_file_text: str
	importance: Optional[float] = None
	helpfulness: Optional[float] = None
	harmfulness: Optional[float] = None
	relevance: Optional[float] = None


class BaseContextBuilder:
	"""Builds prompts from discovered context files."""

	def __init__(self) -> None:
		self.contexts: List[Context] = []

	def search(self, *args, **kwargs) -> List[Context]:
		"""Discover context for the current file and store it in `contexts`.

		Override this in subclasses to implement actual discovery logic. The
		default behavior clears existing contexts and returns the empty list.
		"""

		self.contexts = []
		return self.contexts

	def build(self, limit: Optional[int] = None) -> str:
		"""Sort contexts by relevance and build a prompt from their content."""

		if not self.contexts:
			return ""

		sorted_contexts = sorted(
			self.contexts,
			key=lambda ctx: ctx.relevance,
			reverse=True,
		)

		if limit is not None:
			sorted_contexts = sorted_contexts[:limit]

		parts = []
		for ctx in sorted_contexts:
			parts.append(
				f"Context: {ctx.context_file_name}\n"
				f"Description: {ctx.context_file_description}\n"
				f"Relevance: {ctx.relevance}\n"
				f"---\n"
				f"{ctx.context_file_text}"
			)

		return "\n\n".join(parts)

	def add_context(self, context: Context) -> None:
		"""Add a single context to the builder's context list."""
		self.contexts.append(context)


class GraphContextBuilder(BaseContextBuilder):
	"""Build context from graph_plan.json dependencies for a given file."""


	def __init__(self, root_path: Optional[str] = None, language: str = "python") -> None:
		super().__init__()
		self.root_path = Path(root_path) if root_path else Path(__file__).resolve().parent.parent / "example"
		self.language = language

	def _suffix_for_language(self, language: str) -> str:
		mapping: Dict[str, str] = {
			"python": ".py",
			"py": ".py",
			"javascript": ".js",
			"js": ".js",
			"typescript": ".ts",
			"ts": ".ts",
		}
		return mapping.get(language.lower(), f".{language.lower()}")


	def search(
		self,
		current_node_name: str,
		graph_plan_path: str = "graph_plan.json",
		language: Optional[str] = None,
	) -> List[Context]:
		"""Populate contexts from dependencies of the current file in graph_plan.json."""

		self.contexts = []

		plan_path = Path(graph_plan_path).resolve()
		if not plan_path.exists():
			return self.contexts

		suffix = self._suffix_for_language(language or self.language)

		plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
		nodes = plan_data.get("nodes", [])

		def find_node_by_name(name: str):
			for node in nodes:
				if node.get("name") == name:
					return node
			return None

		current_node = find_node_by_name(current_node_name)
		if not current_node:
			return self.contexts

		depends = current_node.get("depends", []) or []
		if not depends:
			return self.contexts

		for dep_name in depends:
			dep_node = find_node_by_name(dep_name)
			dep_desc = dep_node.get("desc") if dep_node else ""
			dep_file = (self.root_path / f"{dep_name}{suffix}").resolve()
			if not dep_file.exists():
				continue

			context = Context(
				current_file_location=str(self.root_path / f"{current_node_name}{suffix}"),
				current_file_name=current_node_name,
				context_file_location=str(dep_file),
				context_file_name=dep_name,
				context_file_description=dep_desc,
				context_file_text=dep_file.read_text(encoding="utf-8"),
				relevance=1.0,
			)
			self.contexts.append(context)

		return self.contexts

