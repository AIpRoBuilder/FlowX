from pathlib import Path
import ast
import inspect
import importlib.util
import sys
import textwrap
import uuid
from typing import Any, Mapping

from flowx_core.tools.workflow_node_reference import workflow_node_references

# NOTE: ag_ui_workflow.nodes imports are done lazily inside functions below to
# avoid circular imports (nodes.py imports parse_skill_md / extract_skill_commands
# from this module).


def parse_skill_md(text: str) -> dict[str, str]:
    """Parse a skill.md document into a dict keyed by H2 section name.

    Only level-2 headings (``## Heading``) are used as section boundaries.
    The title (H1) is stored under the key ``"_title"``.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        if stripped.startswith("## "):
            if current_key is not None:
                sections[current_key] = "".join(current_lines).strip()
            current_key = stripped[3:].strip()
            current_lines = []
        elif stripped.startswith("# ") and current_key is None:
            # H1 title – store separately
            sections["_title"] = stripped[2:].strip()
        else:
            if current_key is not None:
                current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "".join(current_lines).strip()

    return sections


def extract_skill_commands(section_text: str) -> list[str]:
    """Extract shell commands from fenced code blocks in *section_text*.

    Fences may be ```sh, ```bash, ```shell or plain ```.
    Lines starting with ``$`` have the prefix stripped.
    """
    commands: list[str] = []
    in_block = False
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            cmd = stripped.lstrip("$ ").strip()
            if cmd:
                commands.append(cmd)
    return commands


def _extract_imports_and_body(text: str) -> tuple[list[str], str]:
	"""Extract import statements and return remaining file body.

	Returns:
		(import_statements, body_without_import_statements)
	"""
	try:
		tree = ast.parse(text)
	except SyntaxError:
		return [], text

	lines = text.splitlines(keepends=True)
	import_ranges: list[tuple[int, int]] = []
	import_statements: list[str] = []

	for node in tree.body:
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			start = getattr(node, "lineno", None)
			end = getattr(node, "end_lineno", None)
			if start is None or end is None:
				continue
			import_ranges.append((start, end))
			statement = "".join(lines[start - 1 : end]).strip()
			if statement:
				import_statements.append(statement)

	range_lines: set[int] = set()
	for start, end in import_ranges:
		range_lines.update(range(start, end + 1))

	body_lines = [line for index, line in enumerate(lines, start=1) if index not in range_lines]
	body_text = "".join(body_lines).lstrip("\n")
	return import_statements, body_text


def merge_text_files(
	first_file_path: str,
	second_file_path: str,
	output_file_path: str,
	separator: str = "\n",
) -> str:
	"""Read text from two files, merge them, and write the result to a new file.

	Args:
		first_file_path: Path to the first source text file.
		second_file_path: Path to the second source text file.
		output_file_path: Path to the destination file.
		separator: Text inserted between the two source contents.

	Returns:
		The merged text that was written to ``output_file_path``.
	"""
	first_path = Path(first_file_path)
	second_path = Path(second_file_path)
	output_path = Path(output_file_path)

	first_text = first_path.read_text(encoding="utf-8")
	second_text = second_path.read_text(encoding="utf-8")
	merged_text = f"{first_text}{separator}{second_text}"

	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(merged_text, encoding="utf-8")

	return merged_text


def filter_merge_python_imports(
	first_file_path: str,
	second_file_path: str,
	output_file_path: str,
	body_separator: str = "\n\n",
) -> str:
	"""Merge two Python files and deduplicate imported packages.

	Import statements from both files are extracted, deduplicated while preserving
	their first-seen order, and written once at the top of the merged output.
	The rest of each file body is then appended in order.
	"""
	first_path = Path(first_file_path)
	second_path = Path(second_file_path)
	output_path = Path(output_file_path)

	first_text = first_path.read_text(encoding="utf-8")
	second_text = second_path.read_text(encoding="utf-8")

	first_imports, first_body = _extract_imports_and_body(first_text)
	second_imports, second_body = _extract_imports_and_body(second_text)

	merged_imports = list(dict.fromkeys([*first_imports, *second_imports]))
	body_parts = [part for part in [first_body.strip(), second_body.strip()] if part]

	sections: list[str] = []
	if merged_imports:
		sections.append("\n".join(merged_imports))
	if body_parts:
		sections.append(body_separator.join(body_parts))

	merged_text = "\n\n".join(sections) + "\n"

	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(merged_text, encoding="utf-8")

	return merged_text


def _is_register_decorator(dec: ast.AST) -> bool:
	"""Return True if the decorator AST node represents `register_class`.

	Handles direct names, attributes, and decorator calls (e.g. @register_class(...)).
	"""
	if isinstance(dec, ast.Name):
		return dec.id == "register_class"
	if isinstance(dec, ast.Attribute):
		return getattr(dec, "attr", None) == "register_class"
	if isinstance(dec, ast.Call):
		func = dec.func
		if isinstance(func, ast.Name):
			return func.id == "register_class"
		if isinstance(func, ast.Attribute):
			return getattr(func, "attr", None) == "register_class"
	return False


def find_registered_classes(root_path: str) -> set[str]:
	"""Scan Python files under ``root_path`` and return set of registered class signatures.

	A class is considered registered when it's decorated with ``@register_class``. If the
	class defines a string attribute named ``signature`` that value is used; otherwise the
	class name is used as its signature.
	"""
	# Ensure root_path is a string
	root_path = str(root_path)
	root = Path(root_path)
	signatures: set[str] = set()

	for p in root.rglob("*.py"):
		# skip cache dirs
		if "__pycache__" in p.parts:
			continue
		try:
			text = p.read_text(encoding="utf-8")
			tree = ast.parse(text)
		except Exception:
			continue

		for node in tree.body:
			if not isinstance(node, ast.ClassDef):
				continue
			# detect decorator
			if not any(_is_register_decorator(d) for d in node.decorator_list):
				continue

			signature: str | None = None
			for stmt in node.body:
				# look for simple assignment: signature = "..."
				if isinstance(stmt, ast.Assign):
					for t in stmt.targets:
						if isinstance(t, ast.Name) and t.id == "signature":
							if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
								signature = stmt.value.value
								break
				if signature:
					break

			if not signature:
				signature = node.name

			signatures.add(signature)

	return signatures


def check_registered_class_imports(root_path: str, target_file_path: str) -> list[str]:
	"""Check that ``target_file_path`` imports all registered classes under ``root_path``.

	Returns a sorted list of missing signatures. The check considers explicit
	``from module import Name`` imports and attempts to handle ``from module import *`` by
	loading the target module and treating its exported registered classes as imported.
	Classes defined in the target file itself are not considered missing.
	"""
	# Ensure root_path is a string
	root_path = str(root_path)
	registered = find_registered_classes(root_path)

	target_path = Path(target_file_path)
	try:
		target_text = target_path.read_text(encoding="utf-8")
		target_tree = ast.parse(target_text)
	except Exception:
		return sorted(list(registered))

	imported_names: set[str] = set()
	star_modules: list[str] = []
	classes_defined_in_target: set[str] = set()

	for node in ast.walk(target_tree):
		if isinstance(node, ast.ImportFrom):
			# handle "from X import a, b" and "from X import *"
			module = node.module or ""
			for alias in node.names:
				if alias.name == "*":
					star_modules.append(module)
				else:
					imported_names.add(alias.name)
		elif isinstance(node, ast.Import):
			# imports of modules don't give us direct class names; skip
			continue
		elif isinstance(node, ast.ClassDef):
			# classes defined in the target file satisfy the requirement
			if any(_is_register_decorator(d) for d in node.decorator_list):
				# use signature if present
				sig = None
				for stmt in node.body:
					if isinstance(stmt, ast.Assign):
						for t in stmt.targets:
							if isinstance(t, ast.Name) and t.id == "signature":
								if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
									sig = stmt.value.value
									break
					if sig:
						break
				classes_defined_in_target.add(sig or node.name)

	# handle star imports by loading those modules under root
	imported_via_star: set[str] = set()
	for module in star_modules:
		# try to resolve module to file under root_path
		mod_path = Path(root_path) / Path(module.replace(".", "/") + ".py")
		if mod_path.exists():
			try:
				text = mod_path.read_text(encoding="utf-8")
				tree = ast.parse(text)
				for node in tree.body:
					if isinstance(node, ast.ClassDef) and any(_is_register_decorator(d) for d in node.decorator_list):
						# extract signature
						sig = None
						for stmt in node.body:
							if isinstance(stmt, ast.Assign):
								for t in stmt.targets:
									if isinstance(t, ast.Name) and t.id == "signature":
										if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
											sig = stmt.value.value
											break
								if sig:
									break
						imported_via_star.add(sig or node.name)
			except Exception:
				continue

	missing = set(registered)
	# classes defined in target don't need to be imported
	missing -= classes_defined_in_target
	# directly imported names
	missing -= imported_names
	# those pulled in via star imports
	missing -= imported_via_star

	return sorted(list(missing))



_WORKFLOW_BASE_CLASS_TO_METHODS: dict[str, tuple[str, ...]] | None = None


def _load_workflow_base_class_to_methods() -> dict[str, tuple[str, ...]]:
	try:
		mapping = {
			reference.meta_node_kind: reference.step_output_schema_methods
			for reference in workflow_node_references()
			if reference.step_output_schema_methods
		}
	except Exception:
		return dict()
	return mapping


def _get_workflow_base_class_to_methods() -> dict[str, tuple[str, ...]]:
	global _WORKFLOW_BASE_CLASS_TO_METHODS
	if _WORKFLOW_BASE_CLASS_TO_METHODS is None:
		_WORKFLOW_BASE_CLASS_TO_METHODS = _load_workflow_base_class_to_methods()
	return _WORKFLOW_BASE_CLASS_TO_METHODS


def _get_workflow_base_class_objects() -> dict[str, type]:
	"""Return a mapping of base class name -> class, imported lazily to avoid circular imports."""
	from ag_ui_workflow import nodes as workflow_nodes  # noqa: PLC0415

	class_names = (
		"WorkflowStepNode",
		"WorkflowFileNode",
		"WorkflowOperationNode",
		"WorkflowSkillNode",
		"SpatialTemporalContractNode",
	)
	result: dict[str, type] = {}
	for class_name in class_names:
		base_cls = getattr(workflow_nodes, class_name, None)
		if isinstance(base_cls, type):
			result[class_name] = base_cls
	return result


def _base_name(base: ast.expr) -> str | None:
	if isinstance(base, ast.Name):
		return base.id
	if isinstance(base, ast.Attribute):
		return base.attr
	return None


def _dict_literal_string_keys(expr: ast.AST) -> set[str]:
	if not isinstance(expr, ast.Dict):
		return set()

	keys: set[str] = set()
	for key_node in expr.keys:
		if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
			keys.add(key_node.value)
	return keys


def _find_method(cls: ast.ClassDef, method_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
	for stmt in cls.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method_name:
			return stmt
	return None


def _extract_derived_keys_from_method(method: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
	keys: set[str] = set()
	derived_bindings: dict[str, set[str]] = {}

	def _assign_binding(name: str, binding_keys: set[str]) -> None:
		derived_bindings[name] = set(binding_keys)

	def _extract_keys_from_expr(expr: ast.AST) -> set[str]:
		dict_keys = _dict_literal_string_keys(expr)
		if dict_keys:
			return dict_keys

		if isinstance(expr, ast.Name):
			return set(derived_bindings.get(expr.id, set()))

		return set()

	for node in ast.walk(method):
		if isinstance(node, ast.Assign):
			dict_keys = _extract_keys_from_expr(node.value)
			for target in node.targets:
				if isinstance(target, ast.Name):
					if dict_keys:
						_assign_binding(target.id, dict_keys)
						keys.update(dict_keys)

				if isinstance(target, ast.Subscript):
					if isinstance(target.value, ast.Name) and target.value.id in derived_bindings:
						sub_key = target.slice
						if isinstance(sub_key, ast.Constant) and isinstance(sub_key.value, str):
							keys.add(sub_key.value)
							derived_bindings[target.value.id].add(sub_key.value)

		if isinstance(node, ast.AnnAssign):
			if isinstance(node.target, ast.Name) and node.value is not None:
				dict_keys = _extract_keys_from_expr(node.value)
				if dict_keys:
					_assign_binding(node.target.id, dict_keys)
					keys.update(dict_keys)

		if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
			call = node.value
			if isinstance(call.func, ast.Attribute):
				if isinstance(call.func.value, ast.Name) and call.func.value.id in derived_bindings:
					if call.func.attr == "update" and call.args:
						update_keys = _dict_literal_string_keys(call.args[0])
						if update_keys:
							keys.update(update_keys)
							derived_bindings[call.func.value.id].update(update_keys)

		if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
			call = node.value
			is_step_output = (
				(isinstance(call.func, ast.Name) and call.func.id == "StepRunOutput")
				or (isinstance(call.func, ast.Attribute) and call.func.attr == "StepRunOutput")
			)
			if not is_step_output:
				continue

			for kw in call.keywords:
				if kw.arg != "derived":
					continue
				keys.update(_extract_keys_from_expr(kw.value))

	return keys


def _extract_derived_keys_from_runtime_method(method_obj: Any) -> set[str]:
	try:
		source = textwrap.dedent(inspect.getsource(method_obj))
	except Exception:
		return set()

	try:
		tree = ast.parse(source)
	except Exception:
		return set()

	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			return _extract_derived_keys_from_method(node)
	return set()


def _extract_class_string_constant(cls: ast.ClassDef, attr_name: str) -> str | None:
	for stmt in cls.body:
		if not isinstance(stmt, ast.Assign):
			continue
		for target in stmt.targets:
			if isinstance(target, ast.Name) and target.id == attr_name:
				if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
					return stmt.value.value
	return None


def _ast_preview_text(expr: ast.AST) -> str:
	if isinstance(expr, ast.Name):
		return expr.id
	if isinstance(expr, ast.Attribute):
		return f"{_ast_preview_text(expr.value)}.{expr.attr}"
	if isinstance(expr, ast.Call):
		return f"call:{_ast_preview_text(expr.func)}"
	if isinstance(expr, ast.Subscript):
		return f"{_ast_preview_text(expr.value)}[...]"
	if isinstance(expr, ast.JoinedStr):
		return "formatted_string"
	return expr.__class__.__name__.lower()


def _extract_card_preview_from_expr(expr: ast.AST, bindings: Mapping[str, Any]) -> Any:
	if isinstance(expr, ast.Constant):
		return expr.value

	if isinstance(expr, ast.Name):
		return bindings.get(expr.id, f"<{expr.id}>")

	if isinstance(expr, ast.Dict):
		preview: dict[str, Any] = {}
		for key_node, value_node in zip(expr.keys, expr.values):
			if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
				preview[key_node.value] = _extract_card_preview_from_expr(value_node, bindings)
		return preview

	if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
		return [_extract_card_preview_from_expr(item, bindings) for item in expr.elts]

	if isinstance(expr, ast.JoinedStr):
		return "<formatted_string>"

	if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
		operand = _extract_card_preview_from_expr(expr.operand, bindings)
		if isinstance(operand, (int, float)):
			return -operand
		return f"<expr:{_ast_preview_text(expr)}>"

	return f"<expr:{_ast_preview_text(expr)}>"


def _extract_step_output_card_from_method(method: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any] | None:
	bindings: dict[str, Any] = {}

	for node in ast.walk(method):
		if isinstance(node, ast.Assign):
			preview = _extract_card_preview_from_expr(node.value, bindings)
			for target in node.targets:
				if isinstance(target, ast.Name):
					bindings[target.id] = preview

		if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
			bindings[node.target.id] = _extract_card_preview_from_expr(node.value, bindings)

		if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
			call = node.value
			is_step_output = (
				(isinstance(call.func, ast.Name) and call.func.id == "StepRunOutput")
				or (isinstance(call.func, ast.Attribute) and call.func.attr == "StepRunOutput")
			)
			if not is_step_output:
				continue

			for kw in call.keywords:
				if kw.arg != "card":
					continue
				preview = _extract_card_preview_from_expr(kw.value, bindings)
				if isinstance(preview, dict):
					return preview
				return {"_preview": preview}

	return None


def _extract_step_output_card_from_runtime_method(method_obj: Any) -> dict[str, Any] | None:
	try:
		source = textwrap.dedent(inspect.getsource(method_obj))
	except Exception:
		return None

	try:
		tree = ast.parse(source)
	except Exception:
		return None

	for node in tree.body:
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			return _extract_step_output_card_from_method(node)
	return None


def compile_node_file_and_get_step_output_card_schema(node_file_path: str) -> dict[str, Any] | None:
	"""Parse a node file and return the first StepRunOutput card schema preview.

	The preview is static and AST-based: literal card payload structure is preserved,
	while dynamic expressions are replaced with placeholder strings.
	"""
	path = Path(node_file_path)
	try:
		source = path.read_text(encoding="utf-8")
		tree = ast.parse(source)
	except Exception:
		return None

	workflow_base_methods = _get_workflow_base_class_to_methods()

	for node in tree.body:
		if not isinstance(node, ast.ClassDef):
			continue

		base_names = {_base_name(base) for base in node.bases}
		base_names.discard(None)
		workflow_bases = [base for base in base_names if base in workflow_base_methods]
		if not workflow_bases:
			continue

		method_candidates: list[str] = []
		for workflow_base in workflow_bases:
			for method_name in workflow_base_methods[workflow_base]:
				if method_name not in method_candidates:
					method_candidates.append(method_name)

		for method_name in method_candidates:
			method = _find_method(node, method_name)
			if method is not None:
				card_schema = _extract_step_output_card_from_method(method)
				if card_schema is None:
					continue
				return {
					"step_id": _extract_class_string_constant(node, "STEP_ID") or node.name,
					"title": _extract_class_string_constant(node, "TITLE") or node.name,
					"card": card_schema,
				}

			for workflow_base in workflow_bases:
				fallback_card = _get_workflow_base_method_card_fallbacks().get(workflow_base, {}).get(method_name)
				if fallback_card is None:
					continue
				return {
					"step_id": _extract_class_string_constant(node, "STEP_ID") or node.name,
					"title": _extract_class_string_constant(node, "TITLE") or node.name,
					"card": fallback_card,
				}

	return None


def collect_session_state_keys_from_node_file(node_file_path: str, node_class_name: str) -> set[str]:
	"""Collect string keys accessed on ``session_state`` dict in a node class file.

	Args:
		node_file_path: Path to the node Python file.
		node_class_name: Expected class name for the node.

	Returns:
		Set of session_state keys used in the class.
	"""
	path = Path(node_file_path)
	if not path.is_file():
		return set()

	try:
		source = path.read_text(encoding="utf-8")
		tree = ast.parse(source)
	except Exception:
		return set()

	keys: set[str] = set()

	def _collect_from_class(cls: ast.ClassDef) -> None:
		for node in ast.walk(cls):
			if isinstance(node, ast.Subscript):
				if isinstance(node.value, ast.Name) and node.value.id == "session_state":
					if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
						keys.add(node.slice.value)

			if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
				owner = node.func.value
				if not (isinstance(owner, ast.Name) and owner.id == "session_state"):
					continue

				if node.func.attr in {"get", "pop", "setdefault"} and node.args:
					first_arg = node.args[0]
					if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
						keys.add(first_arg.value)

				if node.func.attr == "update":
					if node.args and isinstance(node.args[0], ast.Dict):
						for key_node in node.args[0].keys:
							if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
								keys.add(key_node.value)
					for keyword in node.keywords:
						if keyword.arg:
							keys.add(keyword.arg)

	class_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
	target_classes = [cls for cls in class_nodes if cls.name == node_class_name] or class_nodes

	for cls in target_classes:
		_collect_from_class(cls)

	return keys


def _load_workflow_base_method_derived_fallbacks() -> dict[str, dict[str, set[str]]]:
	fallbacks: dict[str, dict[str, set[str]]] = {}
	_WORKFLOW_BASE_CLASS_OBJECTS = _get_workflow_base_class_objects()
	for base_name, method_names in _get_workflow_base_class_to_methods().items():
		base_cls = _WORKFLOW_BASE_CLASS_OBJECTS.get(base_name)
		if base_cls is None:
			continue

		base_method_keys: dict[str, set[str]] = {}
		for method_name in method_names:
			method_obj = getattr(base_cls, method_name, None)
			if method_obj is None:
				continue
			base_method_keys[method_name] = _extract_derived_keys_from_runtime_method(method_obj)

		fallbacks[base_name] = base_method_keys

	return fallbacks


def _load_workflow_base_method_card_fallbacks() -> dict[str, dict[str, dict[str, Any] | None]]:
	fallbacks: dict[str, dict[str, dict[str, Any] | None]] = {}
	_WORKFLOW_BASE_CLASS_OBJECTS = _get_workflow_base_class_objects()
	for base_name, method_names in _get_workflow_base_class_to_methods().items():
		base_cls = _WORKFLOW_BASE_CLASS_OBJECTS.get(base_name)
		if base_cls is None:
			continue

		base_method_cards: dict[str, dict[str, Any] | None] = {}
		for method_name in method_names:
			method_obj = getattr(base_cls, method_name, None)
			if method_obj is None:
				continue
			base_method_cards[method_name] = _extract_step_output_card_from_runtime_method(method_obj)

		fallbacks[base_name] = base_method_cards

	return fallbacks


_WORKFLOW_BASE_METHOD_DERIVED_FALLBACKS: dict[str, dict[str, set[str]]] | None = None
_WORKFLOW_BASE_METHOD_CARD_FALLBACKS: dict[str, dict[str, dict[str, Any] | None]] | None = None


def _get_workflow_base_method_derived_fallbacks() -> dict[str, dict[str, set[str]]]:
	global _WORKFLOW_BASE_METHOD_DERIVED_FALLBACKS
	if _WORKFLOW_BASE_METHOD_DERIVED_FALLBACKS is None:
		_WORKFLOW_BASE_METHOD_DERIVED_FALLBACKS = _load_workflow_base_method_derived_fallbacks()
	return _WORKFLOW_BASE_METHOD_DERIVED_FALLBACKS


def _get_workflow_base_method_card_fallbacks() -> dict[str, dict[str, dict[str, Any] | None]]:
	global _WORKFLOW_BASE_METHOD_CARD_FALLBACKS
	if _WORKFLOW_BASE_METHOD_CARD_FALLBACKS is None:
		_WORKFLOW_BASE_METHOD_CARD_FALLBACKS = _load_workflow_base_method_card_fallbacks()
	return _WORKFLOW_BASE_METHOD_CARD_FALLBACKS


def compile_node_file_and_get_derived_keys(node_file_path: str) -> list[str]:
	"""Parse a node file and return all derived-dict keys from workflow node classes.

	The parser inspects subclasses of WorkflowStepNode, WorkflowFileNode,
	WorkflowOperationNode, WorkflowSkillNode, and SpatialTemporalContractNode using Python AST,
	then extracts string keys from local ``derived`` dict literals (and equivalent
	``StepRunOutput(..., derived=...)`` literals) inside relevant methods.
	"""
	path = Path(node_file_path)
	try:
		source = path.read_text(encoding="utf-8")
		tree = ast.parse(source)
	except Exception:
		return []

	all_keys: set[str] = set()
	workflow_base_methods = _get_workflow_base_class_to_methods()

	for node in tree.body:
		if not isinstance(node, ast.ClassDef):
			continue

		if node.name in workflow_base_methods:
			for method_name in workflow_base_methods[node.name]:
				method = _find_method(node, method_name)
				if method is None:
					continue
				all_keys.update(_extract_derived_keys_from_method(method))
			continue

		base_names = {_base_name(base) for base in node.bases}
		base_names.discard(None)

		workflow_bases = [base for base in base_names if base in workflow_base_methods]
		if not workflow_bases:
			continue

		method_candidates: set[str] = set()
		for workflow_base in workflow_bases:
			method_candidates.update(workflow_base_methods[workflow_base])

		for method_name in method_candidates:
			method = _find_method(node, method_name)
			if method is not None:
				all_keys.update(_extract_derived_keys_from_method(method))
				continue

			for workflow_base in workflow_bases:
				fallback = _get_workflow_base_method_derived_fallbacks().get(workflow_base, {}).get(method_name, set())
				all_keys.update(fallback)

	return sorted(all_keys)