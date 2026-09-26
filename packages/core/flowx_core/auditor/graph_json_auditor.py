from __future__ import annotations

import re
from typing import Any, Dict, List, Set

from flowx_core.architect.graph import Graph
from flowx_core.auditor.base_json_auditor import BaseJsonAuditor
from flowx_core.auditor.data import JsonRuleViolation
from flowx_core.tools.workflow_node_reference import (
	canonical_meta_node_kind,
	workflow_meta_node_kind_name_set,
)


class GraphJsonAuditor(BaseJsonAuditor):
	"""Audit graph JSON definitions for name/type consistency."""

	EN_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

	def _extract_ext_type(self, node: Dict[str, Any]) -> str:
		ext_data = node.get("ext_data")
		if isinstance(ext_data, dict):
			return str(ext_data.get("type", "")).strip().lower()
		if isinstance(ext_data, str):
			return ext_data.strip().lower()
		return ""

	def _extract_meta_node_kind(self, node: Dict[str, Any]) -> str:
		return str(node.get("meta_node_kind") or node.get("metaNodeKind") or "").strip()

	def _collect_transitive_dependencies(
		self,
		node_name: str,
		node_by_name: Dict[str, Dict[str, Any]],
	) -> Set[str]:
		"""Collect all upstream dependency node names (multi-hop) for a node."""

		visited: Set[str] = set()
		stack: List[str] = [node_name]

		while stack:
			current = stack.pop()
			current_node = node_by_name.get(current)
			if not isinstance(current_node, dict):
				continue

			depends = current_node.get("depends") or []
			if not isinstance(depends, list):
				continue

			for dep in depends:
				dep_name = str(dep).strip()
				if not dep_name or dep_name in visited:
					continue
				visited.add(dep_name)
				stack.append(dep_name)

		return visited

	def audit_graph_json(self, source: Graph) -> tuple[bool, List[JsonRuleViolation]]:
		"""Validate that every node's ``name`` matches its ``type``, check for cycles, and connectivity.

		Args:
			source: A mapping, JSON string, or path-like object pointing to the
				graph JSON document.

		Returns:
			Tuple of (is_valid, violations) where violations contains any mismatches,
			detected cycles, or disconnected components as ``JsonRuleViolation`` instances.
		"""

		graph = source  # Already a Graph object
		violations: List[JsonRuleViolation] = []
		node_by_name: Dict[str, Dict[str, Any]] = {}

		for node in graph.nodes:
			if not isinstance(node, dict):
				continue
			node_name = str(node.get("name", "")).strip()
			if node_name:
				node_by_name[node_name] = node

		for index, node in enumerate(graph.nodes, start=1):
			if not isinstance(node, dict):
				continue

			name = str(node.get("name", "")).strip()
			node_type = str(node.get("type", "")).strip()
			meta_node_kind = self._extract_meta_node_kind(node)

			if name != node_type:
				violations.append(
					JsonRuleViolation(
						parts_name="graph",
						rule="name_type_mismatch",
						detail=f"Node '{name or '<empty>'}' must have type identical to name, got '{node_type or '<empty>'}'.",
						lineno=index,
					)
				)

			if not self.EN_IDENTIFIER_PATTERN.match(name):
				violations.append(
					JsonRuleViolation(
						parts_name="graph",
						rule="name_not_english_identifier",
						detail=f"Node name '{name or '<empty>'}' must be English-only identifier (letters/digits, starts with letter).",
						lineno=index,
					)
				)

			ext_type = self._extract_ext_type(node)
			if meta_node_kind and meta_node_kind not in workflow_meta_node_kind_name_set():
				violations.append(
					JsonRuleViolation(
						parts_name="graph",
						rule="meta_node_kind_invalid",
						detail=(
							f"Node '{name or '<empty>'}' has unsupported meta_node_kind '{meta_node_kind}'."
						),
						lineno=index,
					)
				)

			if ext_type in {"user_input", "user_file_input", "skill"}:
				expected_meta_node_kind = canonical_meta_node_kind(ext_data=node.get("ext_data"))
				if meta_node_kind and meta_node_kind != expected_meta_node_kind:
					violations.append(
						JsonRuleViolation(
							parts_name="graph",
							rule="meta_node_kind_ext_data_mismatch",
							detail=(
								f"Node '{name or '<empty>'}' has meta_node_kind '{meta_node_kind}' but ext_data.type '{ext_type}' requires '{expected_meta_node_kind}'."
							),
							lineno=index,
						)
					)

			if ext_type == "image":
				violations.append(
					JsonRuleViolation(
						parts_name="graph",
						rule="image_ext_type_unsupported",
						detail=(
							f"Node '{name or '<empty>'}' uses deprecated ext_data.type='image'. "
							"Use supported node kinds such as user_file_input, user_input, skill, or none."
						),
						lineno=index,
					)
				)

		# Check for cycles
		is_acyclic, cycle_path = graph.is_dag()
		if not is_acyclic:
			cycle_display = " -> ".join(cycle_path) if cycle_path else "cycle detected"
			violations.append(
				JsonRuleViolation(
					parts_name="graph",
					rule="cycle_detected",
					detail=f"Graph contains a cycle: {cycle_display}",
					lineno=1,
				)
			)

		# Check for weak connectivity
		is_connected = graph.is_weakly_connected()
		if not is_connected:
			violations.append(
				JsonRuleViolation(
					parts_name="graph",
					rule="disconnected",
					detail="Graph is not fully connected",
					lineno=1,
				)
			)

		return len(violations) == 0, violations
