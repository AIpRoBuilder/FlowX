from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Union

from flowx_core.auditor.base_auditor import BaseAuditor
from flowx_core.auditor.data import RuleViolation
from flowx_core.tools.file_tools import check_registered_class_imports, find_registered_classes


class MainEntryPointAuditor(BaseAuditor):
	"""Audit main entrypoint files for AG-UI lifecycle backend requirements."""

	RouteFunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]

	def audit_main_entrypoint_file(self, file_path: str, nodes_root: str | None = None) -> tuple[bool, List[RuleViolation]]:
		# Ensure nodes_root is a string if provided
		if nodes_root is not None:
			nodes_root = str(nodes_root)

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

		# module-level check: disallow imports inside try blocks
		self._check_no_try_imports(tree, violations)

		if not self._has_import(tree, "fastapi"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="fastapi_import_missing",
					detail="Missing import for fastapi (e.g., 'from fastapi import FastAPI').",
					lineno=1,
				)
			)

		if not self._has_import(tree, "uvicorn"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="uvicorn_import_missing",
					detail="Missing import for uvicorn.",
					lineno=1,
				)
			)

		if not self._has_import(tree, "pydantic"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="pydantic_import_missing",
					detail="Missing import for pydantic (e.g., 'from pydantic import BaseModel').",
					lineno=1,
				)
			)

		if not self._has_import_from_with_names(tree, "fastapi.responses", {"StreamingResponse"}):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="fastapi_response_imports_missing",
					detail="Missing response import from fastapi.responses: StreamingResponse is required.",
					lineno=1,
				)
			)

		if not self._has_import_from_with_names(tree, "ag_ui_workflow", {"WorkflowEngine"}):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="workflow_engine_import_missing",
					detail="Missing 'from ag_ui_workflow import WorkflowEngine'.",
					lineno=1,
				)
			)

		if self._has_import(tree, "argparse"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="argparse_forbidden",
					detail="Lifecycle backend should not rely on argparse CLI plumbing.",
					lineno=1,
				)
			)

		if self._has_relative_import_module(tree, "step_nodes"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="step_nodes_import_forbidden",
					detail="Import node classes from local package __init__.py (e.g. 'from . import NodeA') instead of '.step_nodes'.",
					lineno=1,
				)
			)

		legacy_nodes_root = self._find_global(tree, "NODES_ROOT")
		if legacy_nodes_root is not None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="nodes_root_forbidden",
					detail="NODES_ROOT global should not be defined in main entrypoint.",
					lineno=legacy_nodes_root,
				)
			)

		import_node_modules_lineno = self._find_function(tree, "import_node_modules")
		if import_node_modules_lineno is not None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="import_node_modules_forbidden",
					detail="import_node_modules function should not exist in main entrypoint.",
					lineno=import_node_modules_lineno,
				)
			)

		for global_name in ("PIPELINE_JSON_PATH", "ENGINES", "STEP_CHAIN"):
			global_lineno = self._find_global(tree, global_name)
			if global_lineno is None:
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="lifecycle_global_missing",
						detail=f"Missing required global '{global_name}'.",
						lineno=1,
					)
				)

		run_step_cls = self._find_class(tree, "RunStepInput")
		if run_step_cls is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="run_step_input_model_missing",
					detail="Missing Pydantic model class 'RunStepInput'.",
					lineno=1,
				)
			)
		else:
			self._check_required_model_fields(run_step_cls, {"sessionId", "stepId", "input"}, violations)
			self._check_model_field_not_str_only(run_step_cls, "input", violations)
			self._check_model_has_any_field(run_step_cls, {"file_path", "filePath"}, violations)

		reset_input_cls = self._find_class(tree, "ResetSessionInput")
		if reset_input_cls is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="reset_session_input_model_missing",
					detail="Missing Pydantic model class 'ResetSessionInput'.",
					lineno=1,
				)
			)
		else:
			self._check_required_model_fields(reset_input_cls, {"sessionId"}, violations)

		reset_output_cls = self._find_class(tree, "ResetSessionOutput")
		if reset_output_cls is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="reset_session_output_model_missing",
					detail="Missing Pydantic model class 'ResetSessionOutput'.",
					lineno=1,
				)
			)
		else:
			self._check_required_model_fields(reset_output_cls, {"ok", "sessionId", "threadId", "runId"}, violations)

		get_engine_fn = self._find_function_node(tree, "_get_engine")
		if get_engine_fn is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="get_engine_missing",
					detail="Missing helper function '_get_engine(session_id)'.",
					lineno=1,
				)
			)
		elif not self._function_calls_name(get_engine_fn, "WorkflowEngine"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="workflow_engine_constructor_missing",
					detail="'_get_engine' must construct a WorkflowEngine when no cached engine exists.",
					lineno=get_engine_fn.lineno,
				)
			)

		index_fn = self._find_route_function(tree, method="get", path="/")
		if index_fn is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="index_route_missing",
					detail="Missing GET '/' route.",
					lineno=1,
				)
			)

		run_step_fn = self._find_route_function(tree, method="post", path="/api/run-step")
		cron_start_fn = self._find_route_function(tree, method="post", path="/cron/start")
		if run_step_fn is None and cron_start_fn is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="execution_route_missing",
					detail="Missing execution route: expected POST '/api/run-step' or POST '/cron/start'.",
					lineno=1,
				)
			)
		if run_step_fn is not None:
			if not self._function_calls_name(run_step_fn, "StreamingResponse"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="run_step_streaming_response_missing",
						detail="'/api/run-step' should return StreamingResponse.",
						lineno=run_step_fn.lineno,
					)
				)
		if cron_start_fn is not None:
			if not self._function_calls_attr(cron_start_fn, "_run_all_steps_events"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="cron_start_events_call_missing",
						detail="'/cron/start' should call engine._run_all_steps_events(...).",
						lineno=cron_start_fn.lineno,
					)
				)
			if not self._function_calls_attr(cron_start_fn, "create_task"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="cron_start_background_task_missing",
						detail="'/cron/start' should start the periodic cron runner with asyncio.create_task(...).",
						lineno=cron_start_fn.lineno,
					)
				)
		if run_step_fn is not None:
			if not self._function_calls_attr(run_step_fn, "_run_step_events"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="run_step_events_call_missing",
						detail="'/api/run-step' should call engine._run_step_events(stepId, input).",
						lineno=run_step_fn.lineno,
					)
				)

		reset_fn = self._find_route_function(tree, method="post", path="/api/reset-session")
		if reset_fn is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="reset_session_route_missing",
					detail="Missing POST '/api/reset-session' route.",
					lineno=1,
				)
			)
		else:
			if not self._route_has_keyword(reset_fn, method="post", path="/api/reset-session", keyword="response_model", expected_name="ResetSessionOutput"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="reset_session_response_model_missing",
						detail="'/api/reset-session' should declare response_model=ResetSessionOutput.",
						lineno=reset_fn.lineno,
					)
				)
			if not self._function_calls_attr(reset_fn, "reset_session"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="reset_session_call_missing",
						detail="'/api/reset-session' should call engine.reset_session().",
						lineno=reset_fn.lineno,
					)
				)

		main_guard = self._find_main_guard(tree)
		if main_guard is None or not self._block_has_uvicorn_run_app(main_guard.body):
			lineno = getattr(main_guard, "lineno", 1) if main_guard is not None else 1
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="uvicorn_launcher_missing",
					detail="Missing `if __name__ == \"__main__\": uvicorn.run(app, ...)` launcher.",
					lineno=lineno,
				)
			)

		# Optional: check that the main entrypoint imports all registered node classes
		if nodes_root:
			nodes_root_path = Path(nodes_root).expanduser().resolve()
			expected_module = nodes_root_path.name
			registered_classes = find_registered_classes(str(nodes_root_path))

			if not self._has_import_from_with_names(tree, expected_module, registered_classes):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="nodes_root_import_missing",
						detail=(
							f"Import node classes from '{expected_module}' (e.g. "
							f"'from {expected_module} import NodeA, NodeB')."
						),
						lineno=1,
					)
				)

			if self._has_relative_import_of_names(tree, registered_classes):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="relative_node_import_forbidden",
						detail=(
							"Relative node imports are forbidden. Use "
							f"'from {expected_module} import ...' instead."
						),
						lineno=1,
					)
				)

			missing = check_registered_class_imports(nodes_root, str(path))
			for sig in missing:
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="registered_class_import_missing",
						detail=f"Missing import for registered class '{sig}' from nodes root.",
						lineno=1,
					)
				)

		return len(violations) == 0, violations

	def _has_import_from_with_names(self, tree: ast.AST, module_name: str, required_names: set[str]) -> bool:
		"""Return True if a `from module import ...` includes all required names."""

		if not required_names:
			return True

		collected: set[str] = set()
		for node in tree.body:
			if not isinstance(node, ast.ImportFrom):
				continue
			if node.module != module_name:
				continue
			collected.update(alias.name for alias in node.names)
		return required_names.issubset(collected)

	def _has_import(self, tree: ast.AST, module_name: str) -> bool:
		"""Return True if the module is imported via ``import`` or ``from``."""

		for node in tree.body:
			if isinstance(node, ast.Import):
				for alias in node.names:
					if alias.name.split(".")[0] == module_name:
						return True
			if isinstance(node, ast.ImportFrom):
				if node.module and node.module.split(".")[0] == module_name:
					return True
		return False

	def _has_relative_import_module(self, tree: ast.AST, module_name: str) -> bool:
		"""Return True if file contains relative import from ``.<module_name>``."""

		for node in tree.body:
			if not isinstance(node, ast.ImportFrom):
				continue
			if node.level <= 0:
				continue
			if node.module == module_name:
				return True
		return False

	def _has_relative_import_of_names(self, tree: ast.AST, names: set[str]) -> bool:
		"""Return True when a relative import includes any name in ``names``."""

		if not names:
			return False

		for node in tree.body:
			if not isinstance(node, ast.ImportFrom):
				continue
			if node.level <= 0:
				continue
			for alias in node.names:
				if alias.name == "*" or alias.name in names:
					return True
		return False

	def _find_global(self, tree: ast.AST, name: str) -> int | None:
		"""Return the line number if a global name is defined, otherwise None."""

		for node in tree.body:
			if isinstance(node, ast.Assign):
				for target in node.targets:
					if isinstance(target, ast.Name) and target.id == name:
						return node.lineno
			if isinstance(node, ast.AnnAssign):
				target = node.target
				if isinstance(target, ast.Name) and target.id == name:
					return node.lineno
		return None

	def _check_no_try_imports(self, tree: ast.AST, violations: List[RuleViolation]) -> None:
		"""Ensure the module does not use `try: import` or `try: from ... import`.

		Scans the AST for `Try` nodes that contain `Import` or `ImportFrom`
		statements in their body and emits a violation if any are found.
		"""
		for node in ast.walk(tree):
			if not isinstance(node, ast.Try):
				continue

			for stmt in node.body:
				if isinstance(stmt, (ast.Import, ast.ImportFrom)):
					violations.append(
						RuleViolation(
							class_name="(file)",
							rule="try_import_forbidden",
							detail="Avoid importing inside try blocks; use regular imports instead.",
							lineno=getattr(stmt, "lineno", 0),
						)
					)
					return

	def _find_function(self, tree: ast.AST, name: str) -> int | None:
		"""Return the line number of a top-level function name if present."""

		for node in tree.body:
			if isinstance(node, ast.FunctionDef) and node.name == name:
				return node.lineno
		return None

	def _find_class(self, tree: ast.AST, name: str) -> ast.ClassDef | None:
		for node in tree.body:
			if isinstance(node, ast.ClassDef) and node.name == name:
				return node
		return None

	def _class_field_names(self, class_node: ast.ClassDef) -> set[str]:
		fields: set[str] = set()
		for stmt in class_node.body:
			if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
				fields.add(stmt.target.id)
		return fields

	def _check_required_model_fields(self, class_node: ast.ClassDef, required_fields: set[str], violations: List[RuleViolation]) -> None:
		present = self._class_field_names(class_node)
		missing = sorted(required_fields - present)
		if missing:
			violations.append(
				RuleViolation(
					class_name=class_node.name,
					rule="model_fields_missing",
					detail=f"Model '{class_node.name}' is missing fields: {', '.join(missing)}.",
					lineno=class_node.lineno,
				)
			)

	def _check_model_has_any_field(self, class_node: ast.ClassDef, allowed_fields: set[str], violations: List[RuleViolation]) -> None:
		present = self._class_field_names(class_node)
		if present.intersection(allowed_fields):
			return

		sorted_fields = sorted(allowed_fields)
		violations.append(
			RuleViolation(
				class_name=class_node.name,
				rule="model_fields_missing",
				detail=(
					f"Model '{class_node.name}' must include at least one of fields: "
					f"{', '.join(sorted_fields)}."
				),
				lineno=class_node.lineno,
			)
		)

	def _check_model_field_not_str_only(self, class_node: ast.ClassDef, field_name: str, violations: List[RuleViolation]) -> None:
		for stmt in class_node.body:
			if not isinstance(stmt, ast.AnnAssign):
				continue
			if not isinstance(stmt.target, ast.Name) or stmt.target.id != field_name:
				continue

			annotation = stmt.annotation
			if isinstance(annotation, ast.Name) and annotation.id == "str":
				violations.append(
					RuleViolation(
						class_name=class_node.name,
						rule="model_field_type_mismatch",
						detail=f"Model '{class_node.name}' field '{field_name}' must not be str-only; allow dict/None for file payloads.",
						lineno=stmt.lineno,
					)
				)
				return

			return

		violations.append(
			RuleViolation(
				class_name=class_node.name,
				rule="model_field_type_mismatch",
				detail=f"Model '{class_node.name}' field '{field_name}' must be explicitly typed and support non-str payloads.",
				lineno=class_node.lineno,
			)
		)

	def _find_function_node(self, tree: ast.AST, name: str) -> ast.FunctionDef | None:
		for node in tree.body:
			if isinstance(node, ast.FunctionDef) and node.name == name:
				return node
		return None

	def _find_main_guard(self, tree: ast.AST) -> ast.If | None:
		for node in tree.body:
			if not isinstance(node, ast.If):
				continue
			if self._is_main_guard_test(node.test):
				return node
		return None

	def _is_main_guard_test(self, test: ast.AST) -> bool:
		if not isinstance(test, ast.Compare):
			return False
		if len(test.ops) != 1 or len(test.comparators) != 1:
			return False
		if not isinstance(test.ops[0], ast.Eq):
			return False
		if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
			return False
		comparator = test.comparators[0]
		return isinstance(comparator, ast.Constant) and comparator.value == "__main__"

	def _block_has_uvicorn_run_app(self, statements: list[ast.stmt]) -> bool:
		for stmt in statements:
			for node in ast.walk(stmt):
				if not isinstance(node, ast.Call):
					continue
				if not isinstance(node.func, ast.Attribute):
					continue
				if node.func.attr != "run":
					continue
				if not isinstance(node.func.value, ast.Name) or node.func.value.id != "uvicorn":
					continue
				if self._call_uses_app_argument(node):
					return True
		return False

	def _call_uses_app_argument(self, call: ast.Call) -> bool:
		if call.args:
			first_arg = call.args[0]
			if isinstance(first_arg, ast.Name) and first_arg.id == "app":
				return True
		for keyword in call.keywords:
			if keyword.arg != "app":
				continue
			if isinstance(keyword.value, ast.Name) and keyword.value.id == "app":
				return True
		return False

	def _find_route_function(self, tree: ast.AST, *, method: str, path: str) -> RouteFunctionNode | None:
		for node in tree.body:
			if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				continue
			if self._function_has_route(node, method=method, path=path):
				return node
		return None

	def _function_has_route(self, fn: RouteFunctionNode, *, method: str, path: str) -> bool:
		for dec in fn.decorator_list:
			if not isinstance(dec, ast.Call):
				continue
			if not isinstance(dec.func, ast.Attribute):
				continue
			if dec.func.attr != method:
				continue
			if not dec.args:
				continue
			first = dec.args[0]
			if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value == path:
				return True
		return False

	def _route_has_keyword(self, fn: RouteFunctionNode, *, method: str, path: str, keyword: str, expected_name: str) -> bool:
		for dec in fn.decorator_list:
			if not isinstance(dec, ast.Call):
				continue
			if not isinstance(dec.func, ast.Attribute):
				continue
			if dec.func.attr != method:
				continue
			if not dec.args:
				continue
			first = dec.args[0]
			if not (isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value == path):
				continue
			for kw in dec.keywords:
				if kw.arg != keyword:
					continue
				if isinstance(kw.value, ast.Name) and kw.value.id == expected_name:
					return True
		return False

	def _function_calls_name(self, fn: RouteFunctionNode | None, callee_name: str) -> bool:
		if fn is None:
			return False
		for node in ast.walk(fn):
			if not isinstance(node, ast.Call):
				continue
			if isinstance(node.func, ast.Name) and node.func.id == callee_name:
				return True
		return False

	def _function_calls_attr(self, fn: RouteFunctionNode | None, attr_name: str) -> bool:
		if fn is None:
			return False
		for node in ast.walk(fn):
			if not isinstance(node, ast.Call):
				continue
			if isinstance(node.func, ast.Attribute) and node.func.attr == attr_name:
				return True
		return False

	def _find_async_for_attr_call_lineno(self, fn: RouteFunctionNode, attr_name: str) -> int | None:
		for node in ast.walk(fn):
			if not isinstance(node, ast.AsyncFor):
				continue
			iter_expr = node.iter
			if not isinstance(iter_expr, ast.Call):
				continue
			if not isinstance(iter_expr.func, ast.Attribute):
				continue
			if iter_expr.func.attr == attr_name:
				return node.lineno
		return None

