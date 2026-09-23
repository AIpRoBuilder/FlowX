from __future__ import annotations

import ast
from pathlib import Path
from typing import List

from flowx_core.auditor.base_auditor import BaseAuditor
from flowx_core.auditor.data import RuleViolation




class ContextAuditor(BaseAuditor):
	"""Audit Python files to ensure a class exposes a ``desc`` attribute."""

	def audit_context_file(self, file_path: str) -> tuple[bool, List[RuleViolation]]:
		"""Check the file for classes that define a ``desc`` attribute.

		Accepts either a class attribute (``desc = ...``) or an instance
		attribute assigned to ``self.desc`` inside ``__init__``.
		"""

		path = Path(file_path)
		if not path.is_file():
			raise FileNotFoundError(f"File not found: {path}")

		source = path.read_text(encoding="utf-8")
		try:
			tree = ast.parse(source)
		except SyntaxError as exc:
			violation = RuleViolation(
				class_name="(file)",
				rule="syntax_error",
				detail=str(exc),
				lineno=exc.lineno or 0,
			)
			return False, [violation]

		violations: List[RuleViolation] = []
		class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef)]
		if not class_defs:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="class_missing",
					detail="No classes found to audit.",
					lineno=1,
				)
			)

		for cls in class_defs:
			self._check_desc(cls, violations)

		return len(violations) == 0, violations

	def _check_desc(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
		if self._has_class_attr(cls, "desc"):
			return

		if self._has_init_self_attr(cls, "desc"):
			return

		violations.append(
			RuleViolation(
				class_name=cls.name,
				rule="desc_missing",
				detail="Missing desc attribute (class attr or self.desc in __init__).",
				lineno=cls.lineno,
			)
		)

	def _has_class_attr(self, cls: ast.ClassDef, name: str) -> bool:
		for node in cls.body:
			if isinstance(node, ast.Assign):
				for target in node.targets:
					if isinstance(target, ast.Name) and target.id == name:
						return True
			if isinstance(node, ast.AnnAssign):
				target = node.target
				if isinstance(target, ast.Name) and target.id == name:
					return True
		return False

	def _has_init_self_attr(self, cls: ast.ClassDef, name: str) -> bool:
		init_method = None
		for node in cls.body:
			if isinstance(node, ast.FunctionDef) and node.name == "__init__":
				init_method = node
				break

		if init_method is None:
			return False

		for stmt in init_method.body:
			if isinstance(stmt, ast.Assign):
				for target in stmt.targets:
					if self._is_self_attr(target, name):
						return True
			if isinstance(stmt, ast.AnnAssign):
				if self._is_self_attr(stmt.target, name):
					return True
		return False

	def _is_self_attr(self, target: ast.expr, name: str) -> bool:
		return isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self" and target.attr == name

