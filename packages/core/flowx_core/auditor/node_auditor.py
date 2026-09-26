from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from pydaograph import GParam

from flowx_core.auditor.data import RuleViolation
from flowx_core.auditor.base_auditor import BaseAuditor
from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.tools.file_tools import compile_node_file_and_get_derived_keys
from flowx_core.tools.workflow_node_reference import (
    resolve_workflow_node_reference,
    workflow_node_references,
)


WORKFLOW_NODE_BASE_NAMES = {
    "WorkflowStepNode",
    "WorkflowOperationNode",
    "WorkflowFileNode",
    "WorkflowSkillNode",
}


class NodeAuditor(BaseAuditor):
    """Audit node classes for required methods without calling an LLM."""

    def __post_init__(self) -> None:  # pragma: no cover - deterministic setup
        # Skip the base class client bootstrapping; audits are local and deterministic.
        default_white_list = {"createGParam", "getGParam"}
        self.white_list: Set[str] = set(default_white_list)
        return

    def audit_node_file(
        self,
        file_path: str,
        node_meta: Optional[NodeMeta] = None,
        graph_plan_path: Optional[str] = None,
    ) -> tuple[bool, List[RuleViolation]]:
        """Audit a Python file and return whether it passes plus violations.
        
        Args:
            file_path: Path to the Python file to audit.
            node_meta: Optional NodeMeta object for additional ext_data checks.
            graph_plan_path: Optional graph JSON path used for ancestor session_state checks.
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

        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            violation = RuleViolation(
                class_name="(file)",
                rule="compile_error",
                detail=str(exc),
                lineno=exc.lineno or 0,
            )
            return False, [violation]

        violations: List[RuleViolation] = []
        self._check_no_todo_markers(source, violations)
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

        # collect all local GParam subclasses defined in this module
        subclass_names: Set[str] = set()
        for cls in class_defs:
            for base in cls.bases:
                if isinstance(base, ast.Name) and base.id == "GParam":
                    subclass_names.add(cls.name)
                elif isinstance(base, ast.Attribute) and base.attr == "GParam":
                    subclass_names.add(cls.name)

        workflow_node_subclasses = self._collect_local_workflow_node_subclasses(class_defs)

        for cls in class_defs:
            self._check_workflow_node_subclass_register_class(
                cls,
                workflow_node_subclasses,
                violations,
            )
            self._check_registered_class_name_matches_file_prefix(cls, path, violations)
            self._check_clone(cls, violations)
            self._check_init(cls, violations)
            self._check_step_id_matches_class_name(cls, violations)
            self._check_file_node_no_build_step_output(cls, violations)
            self._check_workflow_step_output_dependency_results(cls, path, violations)
            self._check_session_state_reads_use_ancestor_keys(
                cls=cls,
                node_file_path=path,
                violations=violations,
                graph_plan_path=graph_plan_path,
            )
            self._check_node_meta_base_class_by_ext_data(cls, violations, node_meta)
            self._check_dependencies_match_node_meta(cls, violations, node_meta)
            self._check_self_calls(cls, violations, subclass_names)
            # self._check_node_meta_ext_data(cls, violations, node_meta)
            self._check_state_not_gparam(cls, violations, subclass_names)
            self._check_no_unused_local_variables(cls, violations)
            # self._check_dataclass_is_gparam(cls, violations, subclass_names)
            self._check_no_try_imports(tree, violations)  # New check for try imports

        return len(violations) == 0, violations

    def _check_no_todo_markers(self, source: str, violations: List[RuleViolation]) -> None:
        for lineno, line in enumerate(source.splitlines(), start=1):
            if "todo" in line.lower():
                violations.append(
                    RuleViolation(
                        class_name="(file)",
                        rule="no_todo_markers",
                        detail="TODO markers are not allowed in node files.",
                        lineno=lineno,
                    )
                )

    def _check_clone(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
        if not self._is_registered_class(cls):
            return

        method = self._get_method(cls, "clone")
        if method is None:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="clone_missing",
                    detail="Missing def clone(self) that returns self.",
                    lineno=cls.lineno,
                )
            )
            return

        if len(method.args.args) != 1:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="clone_signature",
                    detail="clone must accept only self.",
                    lineno=method.lineno,
                )
            )

        returns_self = any(
            isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == "self"
            for node in method.body
        )
        if not returns_self:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="clone_return",
                    detail="clone must return self.",
                    lineno=method.lineno,
                )
            )

    def _check_registered_class_name_matches_file_prefix(
        self,
        cls: ast.ClassDef,
        file_path: Path,
        violations: List[RuleViolation],
    ) -> None:
        """Ensure registered class name matches the filename prefix (stem)."""
        if not self._is_registered_class(cls):
            return

        expected_class_name = file_path.stem
        if cls.name != expected_class_name:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="class_name_file_prefix_mismatch",
                    detail=(
                        f"Registered class name must match file prefix '{expected_class_name}', "
                        f"got '{cls.name}'."
                    ),
                    lineno=cls.lineno,
                )
            )

    def _check_init(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
        """Ensure registered classes implement `init(self) -> CStatus`."""
        if not self._is_registered_class(cls):
            return

        method = self._get_method(cls, "init")
        if method is not None:
            if len(method.args.args) != 1:
                violations.append(
                    RuleViolation(
                        class_name=cls.name,
                        rule="init_signature",
                        detail="init must accept only self.",
                        lineno=method.lineno,
                    )
                )

            if not self._has_cstatus_annotation(method):
                violations.append(
                    RuleViolation(
                        class_name=cls.name,
                        rule="init_annotation",
                        detail="init should be annotated to return CStatus.",
                        lineno=method.lineno,
                    )
                )

    def _check_step_id_matches_class_name(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
        """Ensure registered WorkflowStepNode classes define STEP_ID equal to class name."""
        if not self._is_registered_class(cls):
            return
        if not self._is_workflow_step_node_subclass(cls):
            return

        step_id_value: ast.expr | None = None
        step_id_lineno = cls.lineno
        for node in cls.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "STEP_ID":
                        step_id_value = node.value
                        step_id_lineno = node.lineno
                        break
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "STEP_ID":
                    step_id_value = node.value
                    step_id_lineno = node.lineno
            if step_id_value is not None:
                break

        if step_id_value is None:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="step_id_missing",
                    detail="Missing STEP_ID class attribute.",
                    lineno=cls.lineno,
                )
            )
            return

        if not (isinstance(step_id_value, ast.Constant) and isinstance(step_id_value.value, str)):
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="step_id_not_string",
                    detail="STEP_ID must be a string literal equal to the class name.",
                    lineno=step_id_lineno,
                )
            )
            return

        if step_id_value.value != cls.name:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="step_id_mismatch",
                    detail=f"STEP_ID must match class name '{cls.name}'.",
                    lineno=step_id_lineno,
                )
            )

    def _is_workflow_step_node_subclass(self, cls: ast.ClassDef) -> bool:
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in WORKFLOW_NODE_BASE_NAMES:
                return True
            if isinstance(base, ast.Attribute) and base.attr in WORKFLOW_NODE_BASE_NAMES:
                return True
        return False

    def _is_workflow_skill_node_subclass(self, cls: ast.ClassDef) -> bool:
        return self._is_direct_or_attr_base_subclass(cls, {"WorkflowSkillNode"})

    def _is_workflow_input_node_subclass(self, cls: ast.ClassDef) -> bool:
        return self._is_direct_or_attr_base_subclass(cls, {"WorkflowStepNode"})

    def _is_workflow_operation_node_subclass(self, cls: ast.ClassDef) -> bool:
        return self._is_direct_or_attr_base_subclass(cls, {"WorkflowOperationNode"})

    def _is_workflow_file_node_subclass(self, cls: ast.ClassDef) -> bool:
        return self._is_direct_or_attr_base_subclass(cls, {"WorkflowFileNode"})

    def _check_file_node_no_build_step_output(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
        if not self._is_registered_class(cls):
            return
        if not self._is_workflow_file_node_subclass(cls):
            return

        method = self._get_method(cls, "build_step_output")
        if method is not None:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="workflow_file_node_build_step_output_not_allowed",
                    detail=(
                        "WorkflowFileNode subclasses must not implement build_step_output; "
                        "use base WorkflowFileNode output behavior."
                    ),
                    lineno=method.lineno,
                )
            )

    def _workflow_step_output_method_names_for_class(self, cls: ast.ClassDef) -> List[str]:
        base_names: Set[str] = set()
        for base in cls.bases:
            if isinstance(base, ast.Name):
                base_names.add(base.id)
            elif isinstance(base, ast.Attribute):
                base_names.add(base.attr)

        method_names: List[str] = []
        for reference in workflow_node_references():
            if reference.meta_node_kind not in base_names:
                continue
            for method_name in reference.step_output_schema_methods:
                if method_name not in method_names:
                    method_names.append(method_name)
        return method_names

    def _check_workflow_step_output_dependency_results(
        self,
        cls: ast.ClassDef,
        node_file_path: Path,
        violations: List[RuleViolation],
    ) -> None:
        if not self._is_registered_class(cls):
            return

        for method_name in self._workflow_step_output_method_names_for_class(cls):
            method = self._get_method(cls, method_name)
            if method is None:
                continue

            parameter_names = {arg.arg for arg in method.args.args}
            if "dependency_results" not in parameter_names:
                continue

            self._check_dependency_results_usage(
                cls=cls,
                node_file_path=node_file_path,
                method=method,
                violations=violations,
                method_name=method_name,
            )

    def _get_method(self, cls: ast.ClassDef, name: str) -> ast.FunctionDef | None:
        for node in cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def _collect_target_names(self, target: ast.AST) -> Set[str]:
        names: Set[str] = set()
        if isinstance(target, ast.Name):
            names.add(target.id)
            return names
        if isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                names.update(self._collect_target_names(elt))
        return names

    def _check_no_unused_local_variables(self, cls: ast.ClassDef, violations: List[RuleViolation]) -> None:
        if not self._is_workflow_step_node_subclass(cls):
            return

        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            assigned: Dict[str, int] = {}
            used: Set[str] = set()

            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Name) and isinstance(stmt.ctx, ast.Load):
                    used.add(stmt.id)

                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        for name in self._collect_target_names(target):
                            assigned.setdefault(name, stmt.lineno)

                elif isinstance(stmt, ast.AnnAssign):
                    for name in self._collect_target_names(stmt.target):
                        assigned.setdefault(name, stmt.lineno)

                elif isinstance(stmt, ast.AugAssign):
                    for name in self._collect_target_names(stmt.target):
                        assigned.setdefault(name, stmt.lineno)
                        used.add(name)

                elif isinstance(stmt, ast.NamedExpr):
                    for name in self._collect_target_names(stmt.target):
                        assigned.setdefault(name, stmt.lineno)

                elif isinstance(stmt, ast.For):
                    for name in self._collect_target_names(stmt.target):
                        assigned.setdefault(name, stmt.lineno)

                elif isinstance(stmt, ast.With):
                    for item in stmt.items:
                        if item.optional_vars is not None:
                            for name in self._collect_target_names(item.optional_vars):
                                assigned.setdefault(name, stmt.lineno)

                elif isinstance(stmt, ast.ExceptHandler):
                    if isinstance(stmt.name, str) and stmt.name:
                        assigned.setdefault(stmt.name, stmt.lineno)

            for name, lineno in assigned.items():
                if name in {"self", "cls"} or name.startswith("_"):
                    continue
                if name not in used:
                    violations.append(
                        RuleViolation(
                            class_name=cls.name,
                            rule="unused_local_variable",
                            detail=f"Local variable '{name}' is assigned but never used in method '{node.name}'.",
                            lineno=lineno,
                        )
                    )

    def _check_dependencies_match_node_meta(
        self,
        cls: ast.ClassDef,
        violations: List[RuleViolation],
        node_meta: Optional[NodeMeta] = None,
    ) -> None:
        """Ensure DEPENDENCIES class attribute matches node_meta.depends.

        Rules:
        - If node_meta is provided and the class is a registered WorkflowStep-family node,
          a ``DEPENDENCIES`` class attribute must be defined.
        - Its value must be a list of string literals equal to ``node_meta.depends``
          (order-sensitive).
        """
        if node_meta is None:
            return
        if not self._is_registered_class(cls):
            return
        if not self._is_workflow_step_node_subclass(cls):
            return

        expected: List[str] = list(node_meta.depends) if node_meta.depends else []

        # Locate DEPENDENCIES assignment in the class body.
        # Support both:
        #   DEPENDENCIES = [...]
        #   DEPENDENCIES: list[str] = [...]
        deps_value: ast.expr | None = None
        deps_lineno: int = cls.lineno
        for node in cls.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEPENDENCIES":
                        deps_value = node.value
                        deps_lineno = node.lineno
                        break
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "DEPENDENCIES":
                    deps_value = node.value
                    deps_lineno = node.lineno
            if deps_value is not None:
                break

        if deps_value is None:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dependencies_missing",
                    detail=(
                        f"Missing DEPENDENCIES class attribute. "
                        f"Expected: {expected!r} (from node_meta.depends)."
                    ),
                    lineno=cls.lineno,
                )
            )
            return

        # deps_value must be a list of string literals.
        if not isinstance(deps_value, ast.List):
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dependencies_not_list",
                    detail="DEPENDENCIES must be a list literal of string names.",
                    lineno=deps_lineno,
                )
            )
            return

        actual: List[str] = []
        for elt in deps_value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                actual.append(elt.value)
            else:
                violations.append(
                    RuleViolation(
                        class_name=cls.name,
                        rule="dependencies_non_string_element",
                        detail="All elements of DEPENDENCIES must be string literals.",
                        lineno=deps_lineno,
                    )
                )
                return

        if actual != expected:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dependencies_mismatch",
                    detail=(
                        f"DEPENDENCIES {actual!r} does not match node_meta.depends {expected!r}."
                    ),
                    lineno=deps_lineno,
                )
            )

    def _check_node_meta_ext_data(self, cls: ast.ClassDef, violations: List[RuleViolation], node_meta: Optional[NodeMeta] = None) -> None:
        """Check if NodeMeta has ext_data, then class must have getExtData method."""
        if node_meta is None or node_meta.ext_data is None:
            return

        if not self._is_registered_class(cls):
            return

        method = self._get_method(cls, "getExtData")
        if method is None:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="ext_data_getter_missing",
                    detail="NodeMeta has ext_data, but missing def getExtData(self) method.",
                    lineno=cls.lineno,
                )
            )

    def _check_node_meta_base_class_by_ext_data(
        self,
        cls: ast.ClassDef,
        violations: List[RuleViolation],
        node_meta: Optional[NodeMeta] = None,
    ) -> None:
        """Enforce the required workflow base class from canonical node metadata."""
        if node_meta is None:
            return
        if not self._is_registered_class(cls):
            return

        ext_type = ""
        ext_data = node_meta.ext_data
        if isinstance(ext_data, Mapping):
            ext_type = str(ext_data.get("type", "")).strip().lower()
        elif isinstance(ext_data, str):
            ext_type = ext_data.strip().lower()

        if ext_type == "image":
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="ext_data_image_unsupported",
                    detail="ext_data.type='image' is no longer supported.",
                    lineno=cls.lineno,
                )
            )
            return

        expected_reference = resolve_workflow_node_reference(
            meta_node_kind=getattr(node_meta, "meta_node_kind", "") or None,
            ext_data=ext_data,
        )
        expected_base = expected_reference.meta_node_kind
        if self._is_direct_or_attr_base_subclass(cls, {expected_base}):
            return

        detail = (
            f"Node metadata resolves to meta_node_kind '{expected_base}', so the node class must subclass {expected_base}."
        )
        if ext_type:
            detail += f" Current ext_data.type is '{ext_type}'."
        violations.append(
            RuleViolation(
                class_name=cls.name,
                rule="meta_node_kind_requires_matching_base_class",
                detail=detail,
                lineno=cls.lineno,
            )
        )

    def _check_dataclass_is_gparam(
        self,
        cls: ast.ClassDef,
        violations: List[RuleViolation],
        gparam_subclasses: Set[str],
    ) -> None:
        """Check that classes with @dataclass decorator are subclasses of GParam."""
        # Check if the class has @dataclass decorator
        has_dataclass = False
        for decorator in cls.decorator_list:
            target = decorator
            if isinstance(decorator, ast.Call):
                target = decorator.func

            if isinstance(target, ast.Name) and target.id == "dataclass":
                has_dataclass = True
                break
            if isinstance(target, ast.Attribute) and target.attr == "dataclass":
                has_dataclass = True
                break

        if not has_dataclass:
            return

        # Check if this class is a subclass of GParam
        is_gparam_subclass = False
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id == "GParam":
                is_gparam_subclass = True
                break
            elif isinstance(base, ast.Attribute) and base.attr == "GParam":
                is_gparam_subclass = True
                break
            # Also check if it's a subclass of a local GParam subclass
            elif isinstance(base, ast.Name) and base.id in gparam_subclasses:
                is_gparam_subclass = True
                break

        if not is_gparam_subclass:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dataclass_not_gparam",
                    detail="Class with @dataclass decorator must be a subclass of GParam.",
                    lineno=cls.lineno,
                )
            )

    def _check_state_not_gparam(
        self,
        cls: ast.ClassDef,
        violations: List[RuleViolation],
        gparam_subclasses: Set[str],
    ) -> None:
        """Ensure classes whose name contains 'State' are NOT subclasses of GParam.

        This prevents accidentally making state holder classes derive from GParam.
        """
        if "State" not in cls.name:
            return

        # Check if this class is a subclass of GParam or a local GParam subclass
        is_gparam_subclass = False
        for base in cls.bases:
            if isinstance(base, ast.Name) and (base.id == "GParam" or base.id in gparam_subclasses):
                is_gparam_subclass = True
                break
            elif isinstance(base, ast.Attribute) and base.attr == "GParam":
                is_gparam_subclass = True
                break

        if is_gparam_subclass:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="state_gparam_subclass",
                    detail="Classes with 'State' in their name must not subclass GParam.",
                    lineno=cls.lineno,
                )
            )

    def _check_self_calls(
        self,
        cls: ast.ClassDef,
        violations: List[RuleViolation],
        gparam_subclasses: Set[str],
    ) -> None:
        """Examine method bodies for "self" calls and enforce rules.

        Currently this only validates ``self.createGParam`` invocations.  If the
        call is found we make sure that the first argument is an *instance* of a
        class that inherits from :class:`pydaograph.GParam` (a subclass defined
        in the same file) and that the second argument is a string literal.
        Additional self-call checks can be added here in future.
        """
        for node in ast.walk(cls):
            if not isinstance(node, ast.Call):
                continue

            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr == "createGParam"
            ):
                self._validate_create_gparam_call(node, cls.name, violations, gparam_subclasses)

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
                    # one violation is enough for the file
                    return

    def _check_dependency_results_usage(
        self,
        cls: ast.ClassDef,
        node_file_path: Path,
        method: ast.FunctionDef,
        violations: List[RuleViolation],
        method_name: str,
    ) -> None:
        dependencies, dep_lineno = self._get_class_dependencies(cls)
        if not dependencies:
            return

        referenced_deps, derived_accesses, references_all_dependencies = self._collect_dependency_results_accesses(method)
        missing_deps = [] if references_all_dependencies else [dep for dep in dependencies if dep not in referenced_deps]
        if missing_deps:
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dependency_results_missing_dependency_keys",
                    detail=(
                        f"{method_name} must read dependency_results for all DEPENDENCIES. "
                        f"Missing: {missing_deps}."
                    ),
                    lineno=dep_lineno,
                )
            )

        forbidden_field_accesses = self._collect_forbidden_dependency_fields(method)
        for dep_name, field_name, lineno in forbidden_field_accesses:
            if dep_name == "*":
                detail = (
                    f"{method_name} must not use dependency_results[*].{field_name}. "
                    f"Current node should not consume dependent nodes' {field_name}."
                )
            else:
                detail = (
                    f"{method_name} must not use dependency_results['{dep_name}'].{field_name}. "
                    f"Current node should not consume dependent nodes' {field_name}."
                )
            violations.append(
                RuleViolation(
                    class_name=cls.name,
                    rule="dependency_results_summary_card_forbidden",
                    detail=detail,
                    lineno=lineno,
                )
            )

        if not derived_accesses:
            return

        dependency_derived_map = self._load_dependency_derived_keys_map(
            node_file_path=node_file_path,
            dependency_names=dependencies,
            violations=violations,
            class_name=cls.name,
        )

        for dep_name, derived_name, lineno in derived_accesses:
            if dep_name == "*":
                declared_somewhere = any(
                    isinstance(allowed, set) and derived_name in allowed
                    for allowed in dependency_derived_map.values()
                )
                if not declared_somewhere:
                    violations.append(
                        RuleViolation(
                            class_name=cls.name,
                            rule="dependency_results_derived_key_invalid",
                            detail=(
                                f"{method_name} uses dependency_results[*].derived['{derived_name}'], "
                                f"but '{derived_name}' is not declared in any dependency derived keys. Do not use derived key '{derived_name}' that are not declared in the dependency."
                            ),
                            lineno=lineno,
                        )
                    )
                continue

            if dep_name not in dependencies:
                continue
            allowed_derived = dependency_derived_map.get(dep_name)
            if allowed_derived is None:
                continue
            if derived_name not in allowed_derived:
                violations.append(
                    RuleViolation(
                        class_name=cls.name,
                        rule="dependency_results_derived_key_invalid",
                        detail=(
                            f"{method_name} uses dependency_results['{dep_name}'].derived['{derived_name}'], "
                            f"but '{derived_name}' is not declared in {dep_name}'s derived keys. Do not use derived key '{derived_name}' that are not declared in the dependency."
                        ),
                        lineno=lineno,
                    )
                )

    def _get_class_dependencies(self, cls: ast.ClassDef) -> Tuple[List[str], int]:
        for node in cls.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "DEPENDENCIES":
                        values = self._extract_string_list_literal(node.value)
                        return values, node.lineno
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "DEPENDENCIES":
                    values = self._extract_string_list_literal(node.value)
                    return values, node.lineno
        return [], cls.lineno

    def _extract_string_list_literal(self, expr: ast.expr) -> List[str]:
        if not isinstance(expr, (ast.List, ast.Tuple)):
            return []

        result: List[str] = []
        for item in expr.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.strip():
                result.append(item.value)
        return result

    def _collect_dependency_results_accesses(
        self,
        method: ast.FunctionDef,
    ) -> Tuple[Set[str], List[Tuple[str, str, int]], bool]:
        referenced_deps: Set[str] = set()
        dependency_aliases: Dict[str, str] = {}
        derived_aliases: Dict[str, str] = {}
        dependency_value_aliases: Set[str] = set()
        references_all_dependencies = False

        for node in ast.walk(method):
            if self._is_dependency_results_reference(node):
                references_all_dependencies = True

            dep_key = self._extract_dependency_key_expr(node)
            if dep_key is not None:
                referenced_deps.add(dep_key)

            if isinstance(node, ast.For):
                value_aliases = self._extract_dependency_results_value_aliases(node)
                if value_aliases:
                    references_all_dependencies = True
                    dependency_value_aliases.update(value_aliases)

            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                alias_name = node.targets[0].id
                alias_dep = self._extract_dependency_key_expr(node.value)
                if alias_dep is not None:
                    dependency_aliases[alias_name] = alias_dep
                    continue

                alias_derived_dep = self._extract_dependency_derived_expr(node.value)
                if alias_derived_dep is None and isinstance(node.value, ast.Attribute) and node.value.attr == "derived":
                    dep_expr = node.value.value
                    if isinstance(dep_expr, ast.Name):
                        alias_derived_dep = dependency_aliases.get(dep_expr.id)
                        if alias_derived_dep is None and dep_expr.id in dependency_value_aliases:
                            alias_derived_dep = "*"
                if alias_derived_dep is not None:
                    derived_aliases[alias_name] = alias_derived_dep

        derived_accesses: List[Tuple[str, str, int]] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Subscript):
                continue

            derived_name = self._extract_string_subscript_key(node)
            if derived_name is None:
                continue
            if not isinstance(node.value, ast.Attribute) or node.value.attr != "derived":
                continue

            dep_name: Optional[str] = None
            dep_expr = node.value.value
            dep_name = self._extract_dependency_key_expr(dep_expr)
            if dep_name is None and isinstance(dep_expr, ast.Name):
                dep_name = dependency_aliases.get(dep_expr.id)
                if dep_name is None:
                    dep_name = derived_aliases.get(dep_expr.id)
                if dep_name is None and dep_expr.id in dependency_value_aliases:
                    dep_name = "*"
            if dep_name is None:
                continue

            derived_accesses.append((dep_name, derived_name, node.lineno))

        for node in ast.walk(method):
            dep_name, derived_name, lineno = self._extract_dependency_derived_get_call(
                node=node,
                dependency_aliases=dependency_aliases,
                derived_aliases=derived_aliases,
                dependency_value_aliases=dependency_value_aliases,
            )
            if dep_name is None or derived_name is None:
                continue
            derived_accesses.append((dep_name, derived_name, lineno))

        return referenced_deps, derived_accesses, references_all_dependencies

    def _collect_forbidden_dependency_fields(
        self,
        method: ast.FunctionDef,
    ) -> List[Tuple[str, str, int]]:
        dependency_aliases: Dict[str, str] = {}
        dependency_value_aliases: Set[str] = set()

        for node in ast.walk(method):
            if isinstance(node, ast.For):
                dependency_value_aliases.update(self._extract_dependency_results_value_aliases(node))

            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                alias_name = node.targets[0].id
                alias_dep = self._extract_dependency_key_expr(node.value)
                if alias_dep is not None:
                    dependency_aliases[alias_name] = alias_dep

        accesses: List[Tuple[str, str, int]] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in {"card"}:
                continue

            dep_name: Optional[str] = None
            dep_expr = node.value
            dep_name = self._extract_dependency_key_expr(dep_expr)
            if dep_name is None and isinstance(dep_expr, ast.Name):
                dep_name = dependency_aliases.get(dep_expr.id)
                if dep_name is None and dep_expr.id in dependency_value_aliases:
                    dep_name = "*"
            if dep_name is None:
                continue

            accesses.append((dep_name, node.attr, node.lineno))

        return accesses

    def _extract_dependency_derived_expr(self, expr: ast.AST) -> Optional[str]:
        if not isinstance(expr, ast.Attribute) or expr.attr != "derived":
            return None

        dep_expr = expr.value
        dep_name = self._extract_dependency_key_expr(dep_expr)
        if dep_name is not None:
            return dep_name
        return None

    def _extract_dependency_derived_get_call(
        self,
        node: ast.AST,
        dependency_aliases: Dict[str, str],
        derived_aliases: Dict[str, str],
        dependency_value_aliases: Set[str],
    ) -> Tuple[Optional[str], Optional[str], int]:
        if not isinstance(node, ast.Call):
            return None, None, 0

        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and len(node.args) >= 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return None, None, 0

        dep_name = self._extract_dependency_derived_expr(func.value)
        if dep_name is None and isinstance(func.value, ast.Attribute) and func.value.attr == "derived":
            dep_expr = func.value.value
            if isinstance(dep_expr, ast.Name):
                dep_name = dependency_aliases.get(dep_expr.id)
                if dep_name is None and dep_expr.id in dependency_value_aliases:
                    dep_name = "*"
        if dep_name is None and isinstance(func.value, ast.Name):
            dep_name = derived_aliases.get(func.value.id)
        if dep_name is None:
            return None, None, 0

        return dep_name, node.args[0].value, node.lineno

    def _is_dependency_results_reference(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "dependency_results" and isinstance(node.ctx, ast.Load)

    def _extract_dependency_results_value_aliases(self, node: ast.For) -> Set[str]:
        if not isinstance(node.iter, ast.Call):
            return set()

        call = node.iter
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "dependency_results"
            and func.attr in {"values", "items"}
        ):
            return set()

        aliases: Set[str] = set()
        if func.attr == "values" and isinstance(node.target, ast.Name):
            aliases.add(node.target.id)
            return aliases

        if func.attr == "items" and isinstance(node.target, ast.Tuple) and len(node.target.elts) >= 2:
            value_target = node.target.elts[1]
            if isinstance(value_target, ast.Name):
                aliases.add(value_target.id)

        return aliases

    def _extract_dependency_key_expr(self, expr: ast.AST) -> Optional[str]:
        if isinstance(expr, ast.Subscript):
            if isinstance(expr.value, ast.Name) and expr.value.id == "dependency_results":
                return self._extract_string_subscript_key(expr)
            return None

        if isinstance(expr, ast.Call):
            func = expr.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "dependency_results"
                and func.attr == "get"
                and len(expr.args) >= 1
                and isinstance(expr.args[0], ast.Constant)
                and isinstance(expr.args[0].value, str)
            ):
                return expr.args[0].value

        return None

    def _extract_string_subscript_key(self, node: ast.Subscript) -> Optional[str]:
        key_node = node.slice
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            return key_node.value
        return None

    def _load_dependency_derived_keys_map(
        self,
        node_file_path: Path,
        dependency_names: List[str],
        violations: List[RuleViolation],
        class_name: str,
    ) -> Dict[str, Optional[Set[str]]]:
        result: Dict[str, Optional[Set[str]]] = {}
        for dep_name in dependency_names:
            dep_file = self._dependency_file_path(node_file_path=node_file_path, dependency_name=dep_name)
            if not dep_file.is_file():
                violations.append(
                    RuleViolation(
                        class_name=class_name,
                        rule="dependency_file_missing",
                        detail=f"Dependency file not found for '{dep_name}': {dep_file}",
                        lineno=0,
                    )
                )
                result[dep_name] = None
                continue

            derived_keys = compile_node_file_and_get_derived_keys(str(dep_file))
            result[dep_name] = set(derived_keys)
        return result

    def _check_session_state_reads_use_ancestor_keys(
        self,
        cls: ast.ClassDef,
        node_file_path: Path,
        violations: List[RuleViolation],
        graph_plan_path: Optional[str],
    ) -> None:
        if not self._is_registered_class(cls):
            return
        if not self._is_workflow_step_node_subclass(cls):
            return

        allowed_keys = self._load_ancestor_session_state_keys(
            node_name=cls.name,
            node_file_path=node_file_path,
            graph_plan_path=graph_plan_path,
        )
        if allowed_keys is None:
            return

        for method in cls.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for key_name, lineno in self._collect_session_state_read_accesses(method):
                if key_name in allowed_keys:
                    continue
                violations.append(
                    RuleViolation(
                        class_name=cls.name,
                        rule="session_state_ancestor_key_invalid",
                        detail=(
                            f"Method '{method.name}' reads session_state['{key_name}'], "
                            f"but this key is not declared in ancestor nodes. Do not use session_state key '{key_name}' that are not from ancestors."
                        ),
                        lineno=lineno,
                    )
                )

    def _collect_session_state_read_accesses(
        self,
        method: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> List[Tuple[str, int]]:
        aliases: Set[str] = {"session_state"}

        for node in ast.walk(method):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                aliases.add(target.id)

        reads: List[Tuple[str, int]] = []
        for node in ast.walk(method):
            if isinstance(node, ast.Subscript):
                if not isinstance(node.ctx, ast.Load):
                    continue
                if not isinstance(node.value, ast.Name) or node.value.id not in aliases:
                    continue
                key_name = self._extract_string_subscript_key(node)
                if key_name:
                    reads.append((key_name, node.lineno))
                continue

            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Name)
                and func.value.id in aliases
                and len(node.args) >= 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            reads.append((node.args[0].value, node.lineno))

        return reads

    def _load_ancestor_session_state_keys(
        self,
        node_name: str,
        node_file_path: Path,
        graph_plan_path: Optional[str],
    ) -> Optional[Set[str]]:
        graph_path = Path(graph_plan_path) if graph_plan_path else (node_file_path.parent / "graph_plan.json")
        if not graph_path.is_file():
            return None

        try:
            graph = Graph(str(graph_path))
        except Exception:
            return None

        return set(graph.get_ancestor_session_state_keys(node_name, include_current=False))

    def _dependency_file_path(self, node_file_path: Path, dependency_name: str) -> Path:
        return node_file_path.parent / f"{dependency_name}.py"

    def _validate_create_gparam_call(
        self,
        call_node: ast.Call,
        class_name: str,
        violations: List[RuleViolation],
        gparam_subclasses: Set[str],
    ) -> None:
        """Validate arguments passed to ``self.createGParam``.

        ``createGParam`` must be invoked with at least two positional arguments:
        1. an instance of a subclass of :class:`pydaograph.GParam` defined in the
           current module
        2. a string literal used as the parameter name
        """
        lineno = call_node.lineno
        # ensure there are two args
        if len(call_node.args) < 2:
            violations.append(
                RuleViolation(
                    class_name=class_name,
                    rule="createGParam_args",
                    detail="createGParam requires at least two arguments.",
                    lineno=lineno,
                )
            )
            return

        first_arg, second_arg = call_node.args[0], call_node.args[1]

        # first argument must be a call constructing a known subclass
        first_ok = False
        if isinstance(first_arg, ast.Call):
            callee = first_arg.func
            if isinstance(callee, ast.Name) and callee.id in gparam_subclasses:
                first_ok = True

        if not first_ok:
            violations.append(
                RuleViolation(
                    class_name=class_name,
                    rule="createGParam_first_arg",
                    detail="First argument to createGParam should be an instance of a GParam subclass.",
                    lineno=lineno,
                )
            )

        # second argument must be a string literal
        if not (isinstance(second_arg, ast.Constant) and isinstance(second_arg.value, str)):
            violations.append(
                RuleViolation(
                    class_name=class_name,
                    rule="createGParam_second_arg",
                    detail="Second argument to createGParam should be a string literal.",
                    lineno=lineno,
                )
            )

    def _has_cstatus_annotation(self, method: ast.FunctionDef) -> bool:
        annotation = method.returns
        if annotation is None:
            return False

        if isinstance(annotation, ast.Name) and annotation.id == "CStatus":
            return True
        if isinstance(annotation, ast.Attribute) and annotation.attr == "CStatus":
            return True
        return False

    def _collect_local_workflow_node_subclasses(self, class_defs: List[ast.ClassDef]) -> Set[str]:
        """Collect classes derived from an ag_ui_workflow node base."""
        workflow_node_subclasses: Set[str] = set()
        unresolved = {cls.name: cls for cls in class_defs}

        while unresolved:
            progressed = False
            for class_name, cls in list(unresolved.items()):
                if self._has_workflow_node_ancestor(cls, workflow_node_subclasses):
                    workflow_node_subclasses.add(class_name)
                    unresolved.pop(class_name, None)
                    progressed = True

            if not progressed:
                break

        return workflow_node_subclasses

    def _has_workflow_node_ancestor(
        self,
        cls: ast.ClassDef,
        known_workflow_node_subclasses: Set[str],
    ) -> bool:
        for base in cls.bases:
            if isinstance(base, ast.Name):
                if base.id in WORKFLOW_NODE_BASE_NAMES or base.id in known_workflow_node_subclasses:
                    return True
            elif isinstance(base, ast.Attribute):
                if base.attr in WORKFLOW_NODE_BASE_NAMES or base.attr in known_workflow_node_subclasses:
                    return True
        return False

    def _check_workflow_node_subclass_register_class(
        self,
        cls: ast.ClassDef,
        workflow_node_subclasses: Set[str],
        violations: List[RuleViolation],
    ) -> None:
        """Ensure every local workflow node subclass has a @register_class decorator."""
        if cls.name not in workflow_node_subclasses:
            return
        if self._is_registered_class(cls):
            return

        violations.append(
            RuleViolation(
                class_name=cls.name,
                rule="workflow_node_subclass_missing_register_class",
                detail="Workflow node subclasses must be decorated with @register_class.",
                lineno=cls.lineno,
            )
        )

    def _is_registered_class(self, cls: ast.ClassDef) -> bool:
        for decorator in cls.decorator_list:
            target = decorator
            if isinstance(decorator, ast.Call):
                target = decorator.func

            if isinstance(target, ast.Name) and target.id == "register_class":
                return True
            if isinstance(target, ast.Attribute) and target.attr == "register_class":
                return True
        return False

    def _is_direct_or_attr_base_subclass(self, cls: ast.ClassDef, base_names: Set[str]) -> bool:
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in base_names:
                return True
            if isinstance(base, ast.Attribute) and base.attr in base_names:
                return True
        return False
