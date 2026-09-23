from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Set, Tuple

from flowx_core.auditor.data import RuleViolation
from flowx_core.tools.runnable_gnode import RunnableGNode


class BaseAuditor(RunnableGNode):
	"""Audit classes to ensure self.method() calls are defined on the class."""

	def __init__(self) -> None:
		RunnableGNode.__init__(self)
		# Predefined methods that are allowed even if not defined on the class.
		default_white_list = {"createGParam", "getGParam"}
		self.white_list: Set[str] = set(default_white_list)

	def audit_base_file(self, file_path: str) -> tuple[bool, List[RuleViolation]]:
		"""Audit a Python file and report undefined self.<method>() calls."""

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
			self._check_self_calls(cls, violations)

		return len(violations) == 0, violations

	def _check_self_calls(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
		defined_methods = self._collect_defined_methods(cls)

		for call_name, lineno in self._collect_self_calls(cls):
			if call_name in self.white_list:
				continue

			if call_name not in defined_methods:
				violations.append(
					RuleViolation(
						class_name=cls.name,
						rule="undefined_self_call",
						detail=f"self.{call_name}() is called but {call_name} is not defined in the class.",
						lineno=lineno,
					)
				)

	def _collect_defined_methods(self, cls: ast.ClassDef) -> Set[str]:
		return {node.name for node in cls.body if isinstance(node, ast.FunctionDef)}

	def _collect_self_calls(self, cls: ast.ClassDef) -> List[Tuple[str, int]]:
		calls: List[Tuple[str, int]] = []
		for node in ast.walk(cls):
			if isinstance(node, ast.Call):
				func = node.func
				if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self":
					calls.append((func.attr, node.lineno))
		return calls
