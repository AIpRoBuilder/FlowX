from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class WorkflowNodeReference:
	base_class: type
	meta_node_kind: str
	capability_category: str
	input_required: bool
	recommended_ext_data_type: str
	planner_hooks: tuple[str, ...]
	main_utility_methods: tuple[str, ...]
	subclass_implementation_methods: tuple[str, ...]
	step_output_schema_methods: tuple[str, ...]
	summary: str
	step_meta: dict[str, Any]
	supports_inputs_format: bool


_WORKFLOW_NODE_CLASS_NAMES = (
	"WorkflowStepNode",
	"WorkflowOperationNode",
	"WorkflowFileNode",
	"WorkflowSkillNode",
	"SpatialTemporalContractNode",
)


_META_NODE_KIND_TRAITS: dict[str, dict[str, Any]] = {
	"WorkflowStepNode": {
		"capability_category": "input",
		"recommended_ext_data_type": "user_input",
		"supports_inputs_format": True,
	},
	"WorkflowOperationNode": {
		"capability_category": "operation",
		"recommended_ext_data_type": "none",
		"supports_inputs_format": False,
	},
	"WorkflowFileNode": {
		"capability_category": "file",
		"recommended_ext_data_type": "user_file_input",
		"supports_inputs_format": False,
	},
	"WorkflowSkillNode": {
		"capability_category": "skill",
		"recommended_ext_data_type": "skill",
		"supports_inputs_format": True,
	},
	"SpatialTemporalContractNode": {
		"capability_category": "spatial_temporal_contract",
		"recommended_ext_data_type": "spatial_temporal_contract",
		"supports_inputs_format": False,
	},
}


_GUIDANCE_HELPER_NAME_KEYWORDS = ("guidance", "prompt")
_GUIDANCE_HELPER_CONTEXT_PARAMS = frozenset({"session_state", "request_payload"})


@lru_cache(maxsize=1)
def _workflow_nodes_module() -> Any:
	try:
		return import_module("ag_ui_workflow.nodes")
	except Exception as exc:
		raise RuntimeError(
			"ag_ui_workflow.nodes is required to inspect workflow base step metadata. "
			"Install project dependencies first."
		) from exc


@lru_cache(maxsize=1)
def _workflow_tools_module() -> Any | None:
	try:
		return import_module("ag_ui_workflow.tools")
	except Exception:
		return None


def _base_node_classes() -> tuple[type, ...]:
	workflow_nodes = _workflow_nodes_module()
	result: list[type] = []
	for class_name in _WORKFLOW_NODE_CLASS_NAMES:
		base_class = getattr(workflow_nodes, class_name, None)
		if not isinstance(base_class, type):
			raise RuntimeError(
				f"ag_ui_workflow.nodes.{class_name} is unavailable; cannot build workflow node metadata catalog."
			)
		result.append(base_class)
	return tuple(result)


def _step_meta_node_kind(base_class: type, step_meta: Mapping[str, Any]) -> str:
	meta_node_kind = str(step_meta.get("metaNodeKind", "")).strip()
	if meta_node_kind:
		return meta_node_kind
	return str(base_class.meta_node_kind()).strip()


def _traits_for_meta_node_kind(meta_node_kind: str) -> dict[str, Any]:
	return _META_NODE_KIND_TRAITS.get(
		meta_node_kind,
		{
			"capability_category": "operation",
			"recommended_ext_data_type": "none",
			"supports_inputs_format": False,
		},
	)


def _recommended_ext_data_type(meta_node_kind: str) -> str:
	return str(_traits_for_meta_node_kind(meta_node_kind)["recommended_ext_data_type"])


def _supports_inputs_format(meta_node_kind: str) -> bool:
	return bool(_traits_for_meta_node_kind(meta_node_kind)["supports_inputs_format"])


def _unwrap_descriptor(value: Any) -> Any:
	if isinstance(value, (classmethod, staticmethod)):
		return value.__func__
	return value


def _workflow_marker_attr_name(const_name: str, default: str) -> str:
	workflow_tools = _workflow_tools_module()
	if workflow_tools is None:
		return default
	value = getattr(workflow_tools, const_name, "")
	text = str(value).strip()
	return text or default


def _decorator_name(decorator: ast.AST) -> str:
	if isinstance(decorator, ast.Name):
		return decorator.id
	if isinstance(decorator, ast.Attribute):
		return decorator.attr
	if isinstance(decorator, ast.Call):
		return _decorator_name(decorator.func)
	return ""


@lru_cache(maxsize=None)
def _class_method_defs_from_ast(
	base_class: type,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]:
	source_path = inspect.getsourcefile(base_class) or inspect.getfile(base_class)
	if not source_path:
		return ()
	try:
		source = Path(source_path).read_text(encoding="utf-8")
		tree = ast.parse(source)
	except Exception:
		return ()

	class_name = base_class.__name__
	class_def = next(
		(
			node
			for node in ast.walk(tree)
			if isinstance(node, ast.ClassDef) and node.name == class_name
		),
		None,
	)
	if class_def is None:
		return ()

	return tuple(
		stmt
		for stmt in class_def.body
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
	)


def _format_function_signature_from_ast(function_def: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
	arguments = function_def.args
	parameter_names: list[str] = []

	positional = [*arguments.posonlyargs, *arguments.args]
	if positional and positional[0].arg in {"self", "cls"}:
		positional = positional[1:]
	parameter_names.extend(argument.arg for argument in positional)

	if arguments.vararg is not None:
		parameter_names.append(f"*{arguments.vararg.arg}")
	elif arguments.kwonlyargs:
		parameter_names.append("*")

	parameter_names.extend(argument.arg for argument in arguments.kwonlyargs)

	if arguments.kwarg is not None:
		parameter_names.append(f"**{arguments.kwarg.arg}")

	return f"{function_def.name}({', '.join(parameter_names)})"


def _function_parameter_names_from_ast(
	function_def: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
	arguments = function_def.args
	parameter_names: list[str] = []

	positional = [*arguments.posonlyargs, *arguments.args]
	if positional and positional[0].arg in {"self", "cls"}:
		positional = positional[1:]
	parameter_names.extend(argument.arg for argument in positional)

	if arguments.vararg is not None:
		parameter_names.append(arguments.vararg.arg)

	parameter_names.extend(argument.arg for argument in arguments.kwonlyargs)

	if arguments.kwarg is not None:
		parameter_names.append(arguments.kwarg.arg)

	return tuple(parameter_names)


@lru_cache(maxsize=None)
def _class_method_signature_map_from_ast(base_class: type) -> dict[str, str]:
	return {
		function_def.name: _format_function_signature_from_ast(function_def)
		for function_def in _class_method_defs_from_ast(base_class)
	}


@lru_cache(maxsize=None)
def _class_method_parameter_name_map_from_ast(base_class: type) -> dict[str, tuple[str, ...]]:
	return {
		function_def.name: _function_parameter_names_from_ast(function_def)
		for function_def in _class_method_defs_from_ast(base_class)
	}


def _marked_method_names_from_ast(base_class: type, decorator_name: str) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()
	for function_def in _class_method_defs_from_ast(base_class):
		if not any(_decorator_name(decorator) == decorator_name for decorator in function_def.decorator_list):
			continue
		if function_def.name in seen:
			continue
		result.append(function_def.name)
		seen.add(function_def.name)
	return tuple(result)


def _marked_method_names_from_attr(base_class: type, marker_attr: str) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()
	for owner in base_class.__mro__:
		for value in owner.__dict__.values():
			func = _unwrap_descriptor(value)
			marker = getattr(func, marker_attr, None)
			if not isinstance(marker, str):
				continue
			method_name = marker.strip()
			if not method_name or method_name in seen:
				continue
			result.append(method_name)
			seen.add(method_name)
	return tuple(result)


def _direct_self_call_method_names_from_ast(
	function_def: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()

	class _SelfCallVisitor(ast.NodeVisitor):
		def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
			if node is function_def:
				self.generic_visit(node)

		def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
			if node is function_def:
				self.generic_visit(node)

		def visit_Call(self, node: ast.Call) -> None:
			func = node.func
			if (
				isinstance(func, ast.Attribute)
				and isinstance(func.value, ast.Name)
				and func.value.id in {"self", "cls"}
			):
				method_name = func.attr.strip()
				if method_name and method_name not in seen:
					result.append(method_name)
					seen.add(method_name)
			self.generic_visit(node)

	_SelfCallVisitor().visit(function_def)
	return tuple(result)


def _format_signature_from_callable(method_name: str, method_obj: Any) -> str:
	try:
		signature = inspect.signature(method_obj)
	except Exception:
		return method_name

	parameter_names: list[str] = []
	inserted_kwonly_separator = False
	has_vararg = False
	for index, parameter in enumerate(signature.parameters.values()):
		if index == 0 and parameter.name in {"self", "cls"}:
			continue
		if parameter.kind in (
			inspect.Parameter.POSITIONAL_ONLY,
			inspect.Parameter.POSITIONAL_OR_KEYWORD,
		):
			parameter_names.append(parameter.name)
			continue
		if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
			parameter_names.append(f"*{parameter.name}")
			has_vararg = True
			continue
		if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
			if not has_vararg and not inserted_kwonly_separator:
				parameter_names.append("*")
				inserted_kwonly_separator = True
			parameter_names.append(parameter.name)
			continue
		if parameter.kind == inspect.Parameter.VAR_KEYWORD:
			parameter_names.append(f"**{parameter.name}")

	return f"{method_name}({', '.join(parameter_names)})"


def render_workflow_method_signatures(
	base_class: type,
	method_names: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
	signature_map = _class_method_signature_map_from_ast(base_class)
	rendered: list[str] = []
	seen: set[str] = set()
	for method_name in method_names:
		normalized_name = str(method_name).strip()
		if not normalized_name:
			continue
		signature_text = signature_map.get(normalized_name)
		if signature_text is None:
			method_obj = getattr(base_class, normalized_name, None)
			signature_text = _format_signature_from_callable(normalized_name, method_obj)
		if signature_text in seen:
			continue
		rendered.append(signature_text)
		seen.add(signature_text)
	return tuple(rendered)


def _main_utility_methods(base_class: type) -> tuple[str, ...]:
	from_ast = _marked_method_names_from_ast(base_class, "node_main_utility")
	if from_ast:
		return from_ast
	workflow_tools = _workflow_tools_module()
	getter = getattr(workflow_tools, "get_node_main_utility_signature", None) if workflow_tools else None
	if callable(getter):
		try:
			method_name = getter(base_class)
		except Exception:
			method_name = None
		if isinstance(method_name, str) and method_name.strip():
			return (method_name.strip(),)
	return _marked_method_names_from_attr(
		base_class,
		_workflow_marker_attr_name(
			"NODE_MAIN_UTILITY_SIGNATURE_ATTR",
			"__ag_ui_node_main_utility_signature__",
		),
	)


def _subclass_implementation_methods(base_class: type) -> tuple[str, ...]:
	from_ast = _marked_method_names_from_ast(base_class, "node_subclass_implementation")
	if from_ast:
		return from_ast
	workflow_tools = _workflow_tools_module()
	getter = getattr(workflow_tools, "get_node_subclass_implementation_signatures", None) if workflow_tools else None
	if callable(getter):
		try:
			method_names = getter(base_class)
		except Exception:
			method_names = None
		if isinstance(method_names, (list, tuple)):
			normalized = tuple(
				str(name).strip()
				for name in method_names
				if isinstance(name, str) and str(name).strip()
			)
			if normalized:
				return normalized
	return _marked_method_names_from_attr(
		base_class,
		_workflow_marker_attr_name(
			"NODE_SUBCLASS_IMPLEMENTATION_SIGNATURE_ATTR",
			"__ag_ui_node_subclass_implementation_signature__",
		),
	)


def _guidance_helper_priority(method_name: str) -> tuple[int, int]:
	name = method_name.lower()
	if "guidance" in name:
		return (0, len(name))
	if "prompt" in name:
		return (1, len(name))
	return (2, len(name))


@lru_cache(maxsize=None)
def _subclass_guidance_method_names(
	base_class: type,
	subclass_implementation_methods: tuple[str, ...],
) -> tuple[str, ...]:
	method_defs = {
		function_def.name: function_def
		for function_def in _class_method_defs_from_ast(base_class)
	}
	if not method_defs:
		return ()

	root_methods = tuple(
		str(method_name).strip()
		for method_name in subclass_implementation_methods
		if str(method_name).strip()
	)
	if not root_methods:
		return ()

	parameter_map = _class_method_parameter_name_map_from_ast(base_class)
	queue = list(root_methods)
	visited: set[str] = set()
	candidates: list[str] = []
	seen_candidates: set[str] = set()
	discovery_order: dict[str, int] = {}

	while queue:
		current_name = queue.pop(0)
		if current_name in visited:
			continue
		visited.add(current_name)

		function_def = method_defs.get(current_name)
		if function_def is None:
			continue

		for callee_name in _direct_self_call_method_names_from_ast(function_def):
			if callee_name in method_defs and callee_name not in visited:
				queue.append(callee_name)

			if callee_name in root_methods or callee_name in seen_candidates:
				continue

			lower_name = callee_name.lower()
			if not any(keyword in lower_name for keyword in _GUIDANCE_HELPER_NAME_KEYWORDS):
				continue

			parameter_names = set(parameter_map.get(callee_name, ()))
			if not parameter_names.intersection(_GUIDANCE_HELPER_CONTEXT_PARAMS):
				continue

			discovery_order[callee_name] = len(discovery_order)
			candidates.append(callee_name)
			seen_candidates.add(callee_name)

	return tuple(
		sorted(
			candidates,
			key=lambda method_name: (
				_guidance_helper_priority(method_name),
				len(parameter_map.get(method_name, ())),
				discovery_order[method_name],
			),
		)
	)


def render_subclass_guidance_method_signatures(
	base_class: type,
	subclass_implementation_methods: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
	return render_workflow_method_signatures(
		base_class,
		_subclass_guidance_method_names(base_class, tuple(subclass_implementation_methods)),
	)


def _is_step_run_output_call(value: ast.AST | None) -> bool:
	if not isinstance(value, ast.Call):
		return False
	func = value.func
	return (
		isinstance(func, ast.Name) and func.id == "StepRunOutput"
	) or (
		isinstance(func, ast.Attribute) and func.attr == "StepRunOutput"
	)


def _method_returns_step_run_output(method_obj: Any) -> bool:
	try:
		signature = inspect.signature(method_obj)
	except Exception:
		signature = None
	if signature is not None:
		annotation = signature.return_annotation
		if annotation is not inspect.Signature.empty:
			annotation_text = getattr(annotation, "__name__", str(annotation))
			if "StepRunOutput" in annotation_text:
				return True

	try:
		source = textwrap.dedent(inspect.getsource(method_obj))
		tree = ast.parse(source)
	except Exception:
		return False

	function_def = next(
		(
			node
			for node in tree.body
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
		),
		None,
	)
	if function_def is None:
		return False

	step_output_bindings: set[str] = set()
	for node in ast.walk(function_def):
		if isinstance(node, ast.Assign) and _is_step_run_output_call(node.value):
			for target in node.targets:
				if isinstance(target, ast.Name):
					step_output_bindings.add(target.id)
			continue
		if (
			isinstance(node, ast.AnnAssign)
			and isinstance(node.target, ast.Name)
			and _is_step_run_output_call(node.value)
		):
			step_output_bindings.add(node.target.id)
			continue
		if not isinstance(node, ast.Return):
			continue
		if _is_step_run_output_call(node.value):
			return True
		if isinstance(node.value, ast.Name) and node.value.id in step_output_bindings:
			return True
	return False


def _class_defined_method_objects(base_class: type) -> tuple[tuple[str, Any], ...]:
	result: list[tuple[str, Any]] = []
	for name, value in base_class.__dict__.items():
		if name.startswith("__"):
			continue
		func = _unwrap_descriptor(value)
		if callable(func):
			result.append((name, func))
	return tuple(result)


def _step_output_schema_methods(
	base_class: type,
	main_utility_methods: tuple[str, ...],
	subclass_implementation_methods: tuple[str, ...],
) -> tuple[str, ...]:
	result: list[str] = []
	seen: set[str] = set()

	def _append_if_step_output(method_name: str) -> None:
		if not method_name or method_name in seen:
			return
		method_obj = getattr(base_class, method_name, None)
		if method_obj is None or not _method_returns_step_run_output(method_obj):
			return
		result.append(method_name)
		seen.add(method_name)

	for method_name in main_utility_methods:
		_append_if_step_output(method_name)
	if result:
		return tuple(result)

	for method_name in subclass_implementation_methods:
		_append_if_step_output(method_name)
	if result:
		return tuple(result)

	for method_name, method_obj in _class_defined_method_objects(base_class):
		if method_name in seen or not _method_returns_step_run_output(method_obj):
			continue
		result.append(method_name)
		seen.add(method_name)
	return tuple(result)


def _summary_from_step_meta(step_meta: Mapping[str, Any]) -> str:
	for key in ("function", "structure", "implementationGuide"):
		value = str(step_meta.get(key, "")).strip()
		if not value:
			continue
		for line in value.splitlines():
			stripped = line.strip()
			if stripped.startswith("- "):
				return stripped[2:].strip()
			if stripped:
				return stripped
	meta_description = str(step_meta.get("metaDescription", "")).strip()
	return meta_description.splitlines()[0].strip() if meta_description else ""


@lru_cache(maxsize=1)
def workflow_node_references() -> tuple[WorkflowNodeReference, ...]:
	references: list[WorkflowNodeReference] = []
	for base_class in _base_node_classes():
		step_meta = dict(base_class.step_meta())
		meta_node_kind = _step_meta_node_kind(base_class, step_meta)
		traits = _traits_for_meta_node_kind(meta_node_kind)
		main_utility_methods = _main_utility_methods(base_class)
		subclass_implementation_methods = _subclass_implementation_methods(base_class)
		step_output_schema_methods = _step_output_schema_methods(
			base_class,
			main_utility_methods,
			subclass_implementation_methods,
		)
		references.append(
			WorkflowNodeReference(
				base_class=base_class,
				meta_node_kind=meta_node_kind,
				capability_category=str(traits["capability_category"]),
				input_required=bool(step_meta.get("inputRequired", False)),
				recommended_ext_data_type=_recommended_ext_data_type(meta_node_kind),
				planner_hooks=subclass_implementation_methods,
				main_utility_methods=main_utility_methods,
				subclass_implementation_methods=subclass_implementation_methods,
				step_output_schema_methods=step_output_schema_methods,
				summary=_summary_from_step_meta(step_meta),
				step_meta=step_meta,
				supports_inputs_format=_supports_inputs_format(meta_node_kind),
			)
		)
	return tuple(references)


@lru_cache(maxsize=1)
def workflow_meta_node_kind_names() -> tuple[str, ...]:
	return tuple(reference.meta_node_kind for reference in workflow_node_references())


@lru_cache(maxsize=1)
def workflow_meta_node_kind_name_set() -> frozenset[str]:
	return frozenset(workflow_meta_node_kind_names())


def _ext_type_from_value(ext_data: Any) -> str:
	if isinstance(ext_data, Mapping):
		skill_name = str(ext_data.get("skill_name", "")).strip()
		if skill_name:
			return "skill"
		return str(ext_data.get("type", "")).strip().lower()
	if isinstance(ext_data, str):
		return ext_data.strip().lower()
	return ""


def resolve_workflow_node_reference(
	*,
	meta_node_kind: str | None = None,
	ext_data: Any = None,
) -> WorkflowNodeReference:
	meta_lookup = {
		reference.meta_node_kind: reference for reference in workflow_node_references()
	}
	if meta_node_kind:
		matched = meta_lookup.get(str(meta_node_kind).strip())
		if matched is not None:
			return matched

	ext_type = _ext_type_from_value(ext_data)
	for reference in workflow_node_references():
		if reference.recommended_ext_data_type == ext_type:
			return reference

	return meta_lookup["WorkflowOperationNode"]


def canonical_meta_node_kind(*, meta_node_kind: str | None = None, ext_data: Any = None) -> str:
	return resolve_workflow_node_reference(
		meta_node_kind=meta_node_kind,
		ext_data=ext_data,
	).meta_node_kind


def render_workflow_step_meta_catalog() -> str:
	sections: list[str] = []
	for reference in workflow_node_references():
		step_meta = reference.step_meta
		guidance_override_signatures = render_subclass_guidance_method_signatures(
			reference.base_class,
			reference.subclass_implementation_methods,
		)
		parts = [
			f"### {reference.meta_node_kind}",
			f"- capability category: {reference.capability_category}",
			f"- recommended ext_data.type: {reference.recommended_ext_data_type}",
			f"- inputRequired: {str(reference.input_required).lower()}",
			f"- supports inputs_format: {str(reference.supports_inputs_format).lower()}",
			f"- decorator-marked main utility methods: {', '.join(reference.main_utility_methods) if reference.main_utility_methods else 'none'}",
			f"- StepRunOutput card/derived contract methods: {', '.join(reference.step_output_schema_methods) if reference.step_output_schema_methods else 'none'}",
			f"- decorator-marked subclass implementation hooks: {', '.join(reference.subclass_implementation_methods) if reference.subclass_implementation_methods else 'none'}",
			f"- parsed prompt/guidance helper methods from subclass-hook call path: {', '.join(guidance_override_signatures) if guidance_override_signatures else 'none'}",
		]
		parts.extend(
			[
				"- step_meta reference:",
				f"  - metaNodeKind: {step_meta.get('metaNodeKind', reference.meta_node_kind)}",
				f"  - structure: {str(step_meta.get('structure', '')).strip() or 'n/a'}",
				f"  - function: {str(step_meta.get('function', '')).strip() or 'n/a'}",
				f"  - implementationGuide: {str(step_meta.get('implementationGuide', '')).strip() or 'n/a'}",
			]
		)
		sections.append("\n".join(parts))
	return "\n\n".join(sections).strip()


def render_workflow_node_reference(reference: WorkflowNodeReference) -> str:
	step_meta = reference.step_meta
	guidance_override_signatures = render_subclass_guidance_method_signatures(
		reference.base_class,
		reference.subclass_implementation_methods,
	)
	return "\n".join(
		[
			f"### {reference.meta_node_kind}",
			f"- capability category: {reference.capability_category}",
			f"- recommended ext_data.type: {reference.recommended_ext_data_type}",
			f"- inputRequired: {str(reference.input_required).lower()}",
			f"- supports inputs_format: {str(reference.supports_inputs_format).lower()}",
			f"- decorator-marked main utility methods: {', '.join(reference.main_utility_methods) if reference.main_utility_methods else 'none'}",
			f"- StepRunOutput card/derived contract methods: {', '.join(reference.step_output_schema_methods) if reference.step_output_schema_methods else 'none'}",
			f"- decorator-marked subclass implementation hooks: {', '.join(reference.subclass_implementation_methods) if reference.subclass_implementation_methods else 'none'}",
			f"- parsed prompt/guidance helper methods from subclass-hook call path: {', '.join(guidance_override_signatures) if guidance_override_signatures else 'none'}",
			f"- structure: {str(step_meta.get('structure', '')).strip() or 'n/a'}",
			f"- function: {str(step_meta.get('function', '')).strip() or 'n/a'}",
			f"- implementationGuide: {str(step_meta.get('implementationGuide', '')).strip() or 'n/a'}",
		]
	)