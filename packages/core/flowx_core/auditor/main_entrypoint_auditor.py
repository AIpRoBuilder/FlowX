# Modified 2026-10-13: require explicit JSON node-class maps alongside request-scoped lifecycle.
# Modified 2026-10-14: declare the dependent, single-pass main-entrypoint audit step.
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Union

from flowx_core.auditor.base_auditor import BaseAuditor
from flowx_core.auditor.data import RuleViolation
from flowx_core.tools.file_tools import check_registered_class_imports, find_registered_classes


class MainEntryPointAuditor(BaseAuditor):
	"""Audit main entrypoint files for AG-UI lifecycle backend requirements."""

	STEP_ID = "main_audit"
	DEPENDENCIES = ["main_generate"]
	INPUT_REQUIRED = False

	RouteFunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]

	def audit_main_entrypoint_file(self, file_path: str, nodes_root: str | None = None) -> tuple[bool, List[RuleViolation]]:
		"""Backward-compatible name for the main entrypoint audit."""
		return self.audit_file(file_path, nodes_root=nodes_root)

	def audit_file(self, file_path: str, nodes_root: str | None = None) -> tuple[bool, List[RuleViolation]]:
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

		if not self._has_import_from_with_names(tree, "ag_ui_workflow", {"WorkflowPipelinePool"}):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="workflow_pool_import_missing",
					detail="Missing 'from ag_ui_workflow import WorkflowPipelinePool'.",
					lineno=1,
				)
			)
		self._check_runtime_api(tree, violations)

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
					detail="Import node classes from the absolute generated root package, not '.step_nodes'.",
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

		for global_name in ("GRAPH_CONFIG_ID", "PIPELINE_POOL", "ENGINE", "SESSION_REQUESTS", "STEP_CHAIN"):
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
					detail="Missing helper function '_get_engine()' returning the lifespan-managed engine.",
					lineno=1,
				)
			)
		elif self._function_calls_name(get_engine_fn, "WorkflowEngine"):
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="workflow_engine_lazy_creation_forbidden",
					detail="Create the shared engine at lifespan startup, not in '_get_engine'.",
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
		if run_step_fn is None:
			violations.append(
				RuleViolation(
					class_name="(file)",
					rule="execution_route_missing",
					detail="Missing execution route: expected POST '/api/run-step'.",
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
		if run_step_fn is not None:
			if not self._function_calls_attr(run_step_fn, "events"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="run_step_events_call_missing",
						detail="'/api/run-step' should call engine.events(request_id, {step_id: input}).",
						lineno=run_step_fn.lineno,
					)
				)
			else:
				for call in ast.walk(run_step_fn):
					if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "events"):
						continue
					if len(call.args) != 2 or not isinstance(call.args[1], ast.Dict) or len(call.args[1].keys) != 1:
						violations.append(RuleViolation(
							class_name="(file)", rule="run_step_events_arguments_invalid",
							detail="Pass a request ID and a single-step input mapping to engine.events().", lineno=call.lineno,
						))

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
			if not self._function_calls_attr(reset_fn, "close"):
				violations.append(
					RuleViolation(
						class_name="(file)",
						rule="reset_session_close_missing",
						detail="'/api/reset-session' should call engine.close(old_request_id) and rotate this session's request ID.",
						lineno=reset_fn.lineno,
					)
				)
			for call in ast.walk(reset_fn):
				if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "close"):
					continue
				if len(call.args) != 1 or isinstance(call.args[0], ast.Constant) and call.args[0].value is None:
					violations.append(RuleViolation(
						class_name="(file)", rule="reset_session_close_arguments_invalid",
						detail="Reset must close only the old request, never all engine runs or the shared pool.", lineno=call.lineno,
					))
			if not any(isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
				and isinstance(node.value, ast.Name) and node.value.id == "SESSION_REQUESTS" for node in ast.walk(reset_fn)):
				violations.append(RuleViolation(
					class_name="(file)", rule="reset_session_request_rotation_missing",
					detail="Reset must replace SESSION_REQUESTS[session_id] with a new request ID.", lineno=reset_fn.lineno,
				))

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

	def _check_runtime_api(self, tree: ast.Module, violations: List[RuleViolation]) -> None:
		"""Check constructors and lifecycle without inventing removed engine APIs."""
		def report(rule: str, detail: str, node: ast.AST) -> None:
			violations.append(RuleViolation(class_name="(file)", rule=rule, detail=detail, lineno=getattr(node, "lineno", 1)))

		aliases = {"WorkflowEngine": "WorkflowEngine", "WorkflowPipelinePool": "WorkflowPipelinePool"}
		for stmt in tree.body:
			if isinstance(stmt, ast.ImportFrom) and stmt.module == "ag_ui_workflow":
				for alias in stmt.names:
					if alias.name in aliases:
						aliases[alias.asname or alias.name] = alias.name
		calls: dict[str, list[ast.Call]] = {"WorkflowEngine": [], "WorkflowPipelinePool": []}
		for node in ast.walk(tree):
			if isinstance(node, ast.Constant) and node.value == "FLOWX_WORKFLOW_POOL_SIZE":
				report("workflow_pool_env_size_forbidden", "Read pool_size from the workflow config, not FLOWX_WORKFLOW_POOL_SIZE.", node)
			if isinstance(node, ast.Attribute) and node.attr in {"thread_id", "session", "reset_session", "_run_step_events"}:
				report("legacy_workflow_api_forbidden", f"Removed engine API '{node.attr}' is forbidden; use request IDs/events/close.", node)
			if isinstance(node, ast.Attribute) and node.attr == "setWaitFunc":
				report("entrypoint_wait_gate_forbidden", "Wait gates belong in explicit node subclasses, not the main entrypoint.", node)
			if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
				continue
			kind = aliases.get(node.func.id)
			if kind not in calls:
				continue
			calls[kind].append(node)
			keywords = {kw.arg: kw.value for kw in node.keywords}
			if kind == "WorkflowEngine":
				required = {"graph_config_id", "steps_meta", "pool"}
				if node.args or set(keywords) != required:
					report("workflow_engine_constructor_invalid", "WorkflowEngine accepts only graph_config_id, steps_meta and pool keyword arguments.", node)
			else:
				if node.args or set(keywords) != {"pipeline_json", "node_classes", "size"}:
					report("workflow_pool_constructor_invalid", "Generated backends require WorkflowPipelinePool(pipeline_json=native_json_content, node_classes=NODE_CLASSES, size=workflow_config['pool_size']).", node)
				class_map = keywords.get("node_classes")
				if isinstance(class_map, ast.Constant) and class_map.value is None:
					report("workflow_pool_class_map_required", "JSON construction requires an explicit concrete class map keyed by node STEP_ID, not global registration.", node)
				size_arg = keywords.get("size")
				if not (isinstance(size_arg, ast.Subscript) and isinstance(size_arg.value, ast.Name)
					and size_arg.value.id == "workflow_config" and isinstance(size_arg.slice, ast.Constant)
					and size_arg.slice.value == "pool_size"):
					report("workflow_pool_size_config_required", "Pass the validated workflow_config['pool_size'] to the shared pool.", node)
				json_arg = keywords.get("pipeline_json")
				if json_arg is not None:
					path_refs = [ref for ref in ast.walk(json_arg) if isinstance(ref, ast.Name) and ref.id.endswith("_PATH")]
					reads_content = any(isinstance(ref, ast.Attribute) and ref.attr == "read_text" for ref in ast.walk(json_arg))
					literal_path = isinstance(json_arg, ast.Constant) and isinstance(json_arg.value, str) and not json_arg.value.lstrip().startswith(("{", "["))
					if path_refs and not reads_content or literal_path:
						report("workflow_pool_json_content_required", "pipeline_json must be JSON content, not a filename/path.", node)
					if any(isinstance(ref, ast.Name) and ref.id in {"PIPELINE_JSON_PATH", "workflow_config"} for ref in ast.walk(json_arg)):
						report("workflow_pool_planning_config_forbidden", "Derive native node records from imported classes; the FlowX planning config is not native pipeline JSON.", node)

		lifespan_fns = []
		for call in ast.walk(tree):
			if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "FastAPI":
				for kw in call.keywords:
					if kw.arg == "lifespan" and isinstance(kw.value, ast.Name):
						fn = self._find_function_node(tree, kw.value.id)
						if fn is not None:
							lifespan_fns.append(fn)
		if len(lifespan_fns) != 1 or not isinstance(lifespan_fns[0], ast.AsyncFunctionDef):
			report("workflow_lifespan_missing", "FastAPI must declare an async lifespan managing the shared engine and pool.", tree)
			return
		lifespan = lifespan_fns[0]
		lifespan_nodes = set(ast.walk(lifespan))
		yields = [node for node in lifespan_nodes if isinstance(node, ast.Yield)]
		if not yields or not any(isinstance(dec, ast.Name) and dec.id == "asynccontextmanager" for dec in lifespan.decorator_list):
			report("workflow_lifespan_invalid", "Lifespan must be an @asynccontextmanager yielding control to FastAPI.", lifespan)
		for kind, constructors in calls.items():
			if len(constructors) != 1:
				report("workflow_constructor_count_invalid", f"Create exactly one shared {kind} at lifespan startup.", lifespan)
			for constructor in constructors:
				if constructor not in lifespan_nodes or any(constructor.lineno > node.lineno for node in yields):
					report("workflow_constructor_lifetime_invalid", f"{kind} must be created once before lifespan yields, never per session/request.", constructor)

		# Accept both direct close calls and run_in_threadpool(resource.close).
		closes: dict[str, list[ast.Call]] = {"ENGINE": [], "PIPELINE_POOL": []}
		for call in ast.walk(tree):
			if not isinstance(call, ast.Call):
				continue
			callback = call.func
			if isinstance(callback, ast.Name) and callback.id == "run_in_threadpool" and call.args:
				callback = call.args[0]
			if isinstance(callback, ast.Attribute) and callback.attr == "close" and isinstance(callback.value, ast.Name):
				resource = callback.value.id
				if resource == "PIPELINE_POOL" and call not in lifespan_nodes:
					report("workflow_pool_close_lifetime_invalid", "Close the shared pool only at process shutdown in lifespan.", call)
				if resource in closes and call in lifespan_nodes:
					closes[resource].append(call)
		engine_closes, pool_closes = closes["ENGINE"], closes["PIPELINE_POOL"]
		final_nodes = {node for stmt in lifespan_nodes if isinstance(stmt, ast.Try) for final in stmt.finalbody for node in ast.walk(final)}
		if not engine_closes or not pool_closes or any(call not in final_nodes for call in engine_closes + pool_closes):
			report("workflow_shutdown_missing", "Lifespan finally cleanup must close ENGINE then PIPELINE_POOL, including waiting runs.", lifespan)
		elif max(call.lineno for call in engine_closes) >= min(call.lineno for call in pool_closes):
			report("workflow_shutdown_order_invalid", "Close ENGINE before PIPELINE_POOL to return outstanding leases.", lifespan)

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

	def _find_function_node(self, tree: ast.AST, name: str) -> RouteFunctionNode | None:
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
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

