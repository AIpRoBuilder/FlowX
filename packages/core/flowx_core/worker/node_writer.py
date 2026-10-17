# Modified: explicit input-gate contracts for pooled generation and repair.
# Modified: abstract bytes file hooks, explicit persistence, and configured concrete-node prompts.
# Modified: fixed automatic-stage metadata and scoped settings before prompt composition.
# Modified: concrete request-driven generator selects all node contracts from WorkflowParam.state.
import json
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

_WORKFLOW_RUNTIME_POLICY = (
    "AG-UI pooled runtime policy (authoritative):\n"
    "- INPUT_REQUIRED is UI metadata only; base nodes and WorkflowEngine install no wait callback.\n"
    "- Required direct input needs __init__ calling super().__init__() and "
    "self.setWaitFunc(self.wait_function), with wait_function(self) -> bool in the subclass.\n"
    "- Inside the non-blocking callback obtain self.get_workflow_param() after pool attachment "
    "and protect input/state access with param.condition. Return True immediately when "
    "not param.can_run(self.STEP_ID, self.DEPENDENCIES), so failed dependencies cannot strand gates.\n"
    "- Decide readiness from param.inputs using membership (0, False and empty values may be valid). "
    "Apply requirement-defined validation; report "
    "param.set_state(self.STEP_ID, 'running' if ready else 'awaiting_input') and return ready.\n"
    "- Automatic nodes set INPUT_REQUIRED = False and install no input gate. "
    "Do not override run or invent engine/session shims.\n"
    "- Pooled nodes are reused: store request data in session_state/WorkflowParam rather than "
    "instance fields and reset custom event gates per run.\n"
    "- The runtime constructs fresh nodes from JSON-node-name class maps; ordinary undecorated "
    "subclasses need neither register_class nor clone. Optional legacy decorators/clone "
    "may remain, but must not be required.\n"
)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS
from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.tools._node_settings import apply_node_settings
from flowx_core.tools.file_tools import compile_node_file_and_get_derived_keys
from flowx_core.tools.workflow_node_reference import (
    render_workflow_method_signatures,
    render_workflow_node_reference,
    render_workflow_step_meta_catalog,
    resolve_workflow_node_reference,
)


def _build_dependency_derived_context(node_dir: Path, dependency_names: list[str]) -> str:
    if not dependency_names:
        return ""

    lines: list[str] = []
    for dep_name in dependency_names:
        dep_file = node_dir / f"{dep_name}.py"
        if not dep_file.is_file():
            continue

        derived_keys = compile_node_file_and_get_derived_keys(str(dep_file))
        if derived_keys:
            lines.append(f"- {dep_name}:")
            for key in derived_keys:
                lines.append(f"  - dependency_results['{dep_name}'].derived['{key}']")
        else:
            lines.append(f"- {dep_name}: (no derived keys detected)")

    return "\n".join(lines)


def _build_ancestor_session_state_context(graph_plan_path: str, node_name: str) -> str:
    if not graph_plan_path or not node_name:
        return ""

    try:
        graph = Graph(graph_plan_path)
        session_state_keys = graph.get_ancestor_session_state_keys(node_name)
    except Exception:
        return ""

    if not session_state_keys:
        return ""

    return f"- {node_name}: {', '.join(session_state_keys)}"


def _render_dependency_results_context(dependency_results: dict[str, StepRunOutput]) -> str:
    return "\n".join(
        f"- {name}: {json.dumps(dict(result.derived), ensure_ascii=False, default=str)}"
        for name, result in dependency_results.items()
        if isinstance(result.derived, Mapping)
    )


def _render_session_state_context(session_state: dict[str, Any]) -> str:
    values = {
        key: value for key, value in session_state.items()
        if key not in {"node_writer", "build_session"} and not key.endswith("::action")
    }
    return json.dumps(values, ensure_ascii=False, default=str) if values else ""


def _extract_declared_dependencies_from_code(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "DEPENDENCIES":
                    if isinstance(stmt.value, (ast.List, ast.Tuple)):
                        dependencies: list[str] = []
                        for item in stmt.value.elts:
                            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.strip():
                                dependencies.append(item.value)
                        return dependencies
    return []


def _read_node_markdown_reference(
    node_name: str,
    requirement_md_path: Path,
    output_path: Path,
) -> str:
    # Read generated node markdown reference if available.

    # Search order (first existing file is used):
    # 1) <project_root>/node_docs/<node_name>.md
    # 2) <project_root>/<node_name>.md
    # 3) <output_parent>/node_docs/<node_name>.md
    

    filename = f"{node_name}.md"
    requirement_dir = requirement_md_path.resolve().parent
    output_parent = output_path.resolve().parent

    candidates = [
        requirement_dir / "node_docs" / filename,
        requirement_dir / filename,
        output_parent / "node_docs" / filename,
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


def _reference_method_signature_text(reference, method_names: tuple[str, ...]) -> str:
    signatures = render_workflow_method_signatures(reference.base_class, method_names)
    return ", ".join(signatures) if signatures else "none"


def _reference_primary_method_signature(reference, method_names: tuple[str, ...], fallback: str) -> str:
    signatures = render_workflow_method_signatures(reference.base_class, method_names)
    return signatures[0] if signatures else fallback
    

@dataclass
class PromptNodeFileCoderBase(Coder):
    STEP_ID = "node_generate"
    DEPENDENCIES = ["node_plan"]
    INPUT_REQUIRED = False

    prompt_path: str = "worker/prompts/pydaograph_node_prompt.md"

    def __post_init__(self) -> None:
        apply_node_settings(self)
        prompt_file = ROOT_DIR / self.prompt_path
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        workflow_step_meta_catalog_text = render_workflow_step_meta_catalog()
        # Preserve an explicitly configured or already composed pool snapshot.
        if self.system_prompt == Coder.system_prompt:
            self.system_prompt = (
                f"{prompt_file.read_text(encoding='utf-8')}\n\n"
                "## ag_ui_workflow Base Step Metas (Authoritative)\n"
                "Use the injected ag_ui_workflow base-node step_meta()/meta_node_kind() catalog below as the only source of truth for node contracts.\n\n"
                f"{workflow_step_meta_catalog_text}\n\n"
                f"{_WORKFLOW_RUNTIME_POLICY}"
            )
        super().__post_init__()

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        """Dispatch a node write or amendment from the session request and live upstream state."""
        del user_input
        request = session_state.get("node_writer")
        if not isinstance(request, Mapping):
            raise ValueError("session_state['node_writer'] must be a write or amend request")

        action_key = f"{type(self).__name__}::action"
        action = session_state.get(action_key)
        if action not in ("write", "amend"):
            raise ValueError(f"{action_key} must be 'write' or 'amend'")

        dependency_context = _render_dependency_results_context(dependency_results)
        ancestor_context = _render_session_state_context(session_state)
        graph_plan_path = request.get("graph_plan_path", "")
        language = request.get("language", "python")
        overwrite = request.get("overwrite", True)
        max_tokens = request.get("max_tokens", MAX_TOKENS)
        root_dir_path = request.get("root_dir_path", "")
        additional_generation_context = request.get("additional_generation_context", "")

        if action == "write":
            node_name = request.get("node_name")
            requirement_path = request.get("requirement_md_path")
            output_path = request.get("output_path")
            if not isinstance(node_name, str) or not node_name.strip():
                raise ValueError("write requires a non-empty node_name")
            if not isinstance(requirement_path, str) or not requirement_path.strip():
                raise ValueError("write requires a non-empty requirement_md_path")
            if not isinstance(output_path, str) or not output_path.strip():
                raise ValueError("write requires a non-empty output_path")

            node_meta = request.get("node_meta")
            if isinstance(node_meta, Mapping):
                node_meta = NodeMeta.from_dict(node_meta)
            if node_meta is None and graph_plan_path:
                node_meta = Graph(graph_plan_path).get_node_meta(node_name)
            if not isinstance(node_meta, NodeMeta):
                raise ValueError("write requires node_meta or a graph_plan_path containing node_name")

            written = self.write_node_from_requirement(
                node_name, node_meta, requirement_path, output_path,
                graph_plan_path=graph_plan_path, language=language,
                overwrite=overwrite, temperature=request.get("temperature", 0.2),
                max_tokens=max_tokens, dependency_context=dependency_context,
                ancestor_session_state_context=ancestor_context,
                root_dir_path=root_dir_path,
                additional_generation_context=additional_generation_context,
            )
        else:
            code_path = request.get("code_path", request.get("output_path"))
            amendment = request.get("amendment")
            if not isinstance(code_path, str) or not code_path.strip():
                raise ValueError("amend requires a non-empty code_path")
            if not isinstance(amendment, str) or not amendment.strip():
                raise ValueError("amend requires a non-empty amendment")
            written = self.amend_code_with_feedback(
                code_path, amendment,
                graph_plan_path=graph_plan_path,
                current_node_name=request.get("node_name", ""),
                language=language, overwrite=overwrite,
                temperature=request.get("temperature", 0.3),
                max_tokens=max_tokens, dependency_context=dependency_context,
                ancestor_session_state_context=ancestor_context,
                additional_generation_context=additional_generation_context,
            )

        return StepRunOutput(derived={"generated_path": str(written)})

    @staticmethod
    def _language_extension(language: str) -> str:
        language_clean = language.strip().lower() if language else "python"
        ext_map = {
            "python": ".py",
            "py": ".py",
            "javascript": ".js",
            "js": ".js",
            "typescript": ".ts",
            "ts": ".ts",
            "java": ".java",
            "go": ".go",
            "golang": ".go",
            "csharp": ".cs",
            "c#": ".cs",
        }
        return ext_map.get(language_clean, f".{language_clean}" if language_clean else ".txt")

    @staticmethod
    def _format_ext_data(ext_data: Any) -> str:
        if not ext_data:
            return "none"
        if isinstance(ext_data, Mapping):
            return json.dumps(dict(ext_data), ensure_ascii=False)
        return str(ext_data)

    @staticmethod
    def _format_depends(depends: list[str] | None) -> str:
        return ", ".join(depends) if depends else "none"

    @staticmethod
    def _format_inputs_format(inputs_format: Mapping[str, Any] | None) -> str:
        if not isinstance(inputs_format, Mapping) or not inputs_format:
            return "none"
        normalized: dict[str, str] = {}
        for key, value in inputs_format.items():
            field_name = str(key).strip()
            field_type = str(value).strip()
            if field_name and field_type:
                normalized[field_name] = field_type
        return json.dumps(normalized, ensure_ascii=False) if normalized else "none"

    @staticmethod
    def _has_default_config_audit_rule(amendment: str) -> bool:
        if not amendment:
            return False
        text = amendment.lower()
        return (
            "dependency_results_missing_dependency_keys" in text
            or "session_state_ancestor_key_invalid" in text
        )

    def _build_requirement_prompt(
        self,
        node_name: str,
        node_meta: NodeMeta,
        requirement_text: str,
        node_markdown_reference: str,
        output_path: str,
        graph_plan_path: str,
        language_clean: str,
        node_contract_text: str,
        dependency_context: str | None = None,
        ancestor_session_state_context: str | None = None,
        root_dir_path: str = "",
        additional_generation_context: str = "",
    ) -> str:
        del root_dir_path
        selected_reference = resolve_workflow_node_reference(
            meta_node_kind=node_meta.meta_node_kind or None,
            ext_data=node_meta.ext_data,
        )
        selected_subclass_hook = _reference_primary_method_signature(
            selected_reference,
            selected_reference.subclass_implementation_methods,
            "the selected base-node subclass hook",
        )
        depends_text = self._format_depends(node_meta.depends)
        ext_data_text = self._format_ext_data(node_meta.ext_data)
        inputs_format_text = self._format_inputs_format(node_meta.inputs_format)

        minimal_policy_text = (
            "Minimal implementation policy (must follow):\n"
            "- Implement only requirement-critical behavior for this node; no extra features.\n"
            "- Keep structure flat and concise; avoid unnecessary helper functions/classes.\n"
            "- Prefer straightforward parsing/validation with simple guard clauses.\n"
            "- Every defined variable must be used; remove dead assignments.\n"
            "- Add detailed debug logging that writes to a local file path so node execution can be diagnosed after runs.\n"
            "- Keep the logging implementation simple and self-contained; prefer a module-local logger, FileHandler, and path creation via pathlib.\n"
            "- Do not add demo/example code, tests, or comments unless required.\n"
            "- Do not include TODO markers in generated code.\n\n"
        )

        state_routing_policy_text = (
            "State routing policy (authoritative):\n"
            "- If a value should be shared globally or reused across non-immediate downstream steps, store/update it in session_state.\n"
            "- If a value is only intended for downstream child-step passing, put it in StepRunOutput.derived.\n"
            "- Do not store child-step-only transit values in session_state unless long-lived/global reuse is explicitly required.\n"
            "- For values not found in session_state and dependency_results[dep].derived, apply safe fallback handling with explicit validation.\n"
            "- If a required value is still missing/invalid after fallback, return an explicit validation error.\n\n"
        )

        user_prompt = (
            "You are generating an AG-UI workflow step node for PyDaoGraph.\n"
            f"Node name: {node_name}\n"
            f"Type: {node_meta.type}\n"
            f"Description: {node_meta.desc}\n"
            f"meta_node_kind: {selected_reference.meta_node_kind}\n"
            f"Depends on: {depends_text}\n"
            f"External data: {ext_data_text}\n"
            f"User inputs format: {inputs_format_text}\n"
            f"Expected base class: {selected_reference.meta_node_kind}\n"
            f"Target language: {language_clean}\n\n"
            f"{minimal_policy_text}"
            f"{state_routing_policy_text}"
            "Selected base-node reference from ag_ui_workflow step_meta() (authoritative):\n"
            f"{render_workflow_node_reference(selected_reference)}\n\n"
            f"{node_contract_text}"
            "Requirement analysis that this node should satisfy:\n"
            f"{requirement_text}\n\n"
        )

        ext_data = node_meta.ext_data if isinstance(node_meta.ext_data, Mapping) else {}
        ext_type = str(ext_data.get("type", "none")).strip().lower()
        if ext_type == "user_input" and inputs_format_text != "none":
            user_prompt += (
                "Input schema constraints (authoritative):\n"
                f"- This node is ext_data.type='{ext_type}' with explicit inputs_format.\n"
                f"- inputs_format: {inputs_format_text}\n"
                f"- In {selected_subclass_hook}, parse/validate user_input against this schema and produce structured derived fields using the same keys when reasonable.\n\n"
            )

        if node_markdown_reference:
            user_prompt += (
                "Node-specific markdown reference (authoritative for this node's implementation focus):\n"
                f"{node_markdown_reference}\n\n"
            )

        if dependency_context is None:
            dependency_context = _build_dependency_derived_context(
                node_dir=Path(output_path).expanduser().resolve().parent,
                dependency_names=list(node_meta.depends or []),
            )
        if dependency_context:
            user_prompt += (
                "\n\nDependency derived context (authoritative):\n"
                f"{dependency_context}\n"
                "- Read dependency_results only from the dependency ids and derived keys listed here.\n"
                "- Never invent dependency derived keys outside this context.\n"
            )

        if ancestor_session_state_context is None:
            ancestor_session_state_context = _build_ancestor_session_state_context(
                graph_plan_path=graph_plan_path,
                node_name=node_name,
            )
        if ancestor_session_state_context:
            user_prompt += (
                "\n\nAncestor session_state context (authoritative):\n"
                f"{ancestor_session_state_context}\n"
                "- Read only the available session_state keys listed here; do not invent upstream keys.\n"
                "- New keys may be written when needed for cross-step state; validate missing required values.\n"
            )

        if user_prompt.strip():
            user_prompt += (
                f"\n\nReturn only runnable {language_clean} code for this node."
                " Do not include markdown fences or explanation text."
            )
        if additional_generation_context.strip():
            user_prompt += (
                "\n\nAdditional node-generation context (authoritative; preserve the current node contract):\n"
                f"{additional_generation_context}\n"
            )

        return user_prompt

    def write_node_from_requirement(
        self,
        node_name: str,
        node_meta: NodeMeta,
        requirement_md_path: str,
        output_path: str,
        *,
        graph_plan_path: str = "",
        language: str = "python",
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
        dependency_context: str | None = None,
        ancestor_session_state_context: str | None = None,
        root_dir_path: str = "",
        additional_generation_context: str = "",
    ) -> Path:
        requirement_path = Path(requirement_md_path)
        if not requirement_path.exists():
            raise FileNotFoundError(f"Requirement file not found: {requirement_path}")

        requirement_text = requirement_path.read_text(encoding="utf-8")
        node_markdown_reference = _read_node_markdown_reference(
            node_name=node_name,
            requirement_md_path=requirement_path,
            output_path=Path(output_path),
        )

        language_clean = language.strip().lower() if language else "python"
        target_ext = self._language_extension(language_clean)

        user_prompt = self._build_requirement_prompt(
            node_name=node_name,
            node_meta=node_meta,
            requirement_text=requirement_text,
            node_markdown_reference=node_markdown_reference,
            output_path=output_path,
            graph_plan_path=graph_plan_path,
            language_clean=language_clean,
            node_contract_text=self.get_node_contract_text(),
            dependency_context=dependency_context,
            ancestor_session_state_context=ancestor_session_state_context,
            root_dir_path=root_dir_path,
            additional_generation_context=additional_generation_context,
        )

        target_path = Path(output_path)
        if target_path.suffix.lower() != target_ext:
            target_path = target_path.with_suffix(target_ext)

        return self.code_to_file(
            user_prompt,
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _build_amendment_prompt(
        self,
        original_code: str,
        amendment: str,
        language_clean: str,
        contract_text: str,
        additional_generation_context: str = "",
    ) -> str:
        state_routing_policy_text = (
            "State routing policy (authoritative):\n"
            "- If a value should be shared globally or reused across non-immediate downstream steps, store/update it in session_state.\n"
            "- If a value is only intended for downstream child-step passing, put it in StepRunOutput.derived.\n"
            "- Do not store child-step-only transit values in session_state unless long-lived/global reuse is explicitly required.\n"
            "- If a required value is missing/invalid after resolution attempts, return an explicit validation error.\n\n"
        )

        user_prompt = (
            "You are updating an existing AG-UI workflow node implementation.\n"
            f"Target language: {language_clean}\n"
            "Keep the code as simple as possible while fully satisfying the amendment and node requirements.\n"
            "Avoid adding new abstractions unless they are strictly needed.\n"
            "Every defined variable must be used; remove dead assignments.\n"
            f"{_WORKFLOW_RUNTIME_POLICY}"
            f"{state_routing_policy_text}"
            f"{contract_text}"
            "Apply the amendment or feedback to produce the improved code.\n"
            "Return only runnable code without commentary or markdown fences.\n\n"
            "Existing code:\n"
            f"{original_code}\n\n"
            "Amendment / feedback to apply:\n"
            f"{amendment}\n"
        )

        if self._has_default_config_audit_rule(amendment):
            user_prompt += (
                "\nAudit remediation rule (authoritative):\n"
                "- If audit reports dependency_results_missing_dependency_keys or session_state_ancestor_key_invalid, treat missing variables as potential config variables.\n"
                "- Add/ensure class-level DEFAULT_CONFIG = { ... } in the node file with placeholders for those missing keys.\n"
                "- If the value is still missing/invalid after resolution attempts, return an explicit validation error.\n"
            )
        if additional_generation_context.strip():
            user_prompt += (
                "\n\nAdditional node-generation context (authoritative; preserve the current node contract):\n"
                f"{additional_generation_context}\n"
            )

        return user_prompt

    def amend_code_with_feedback(
        self,
        code_path: str,
        amendment: str,
        *,
        graph_plan_path: str = "",
        requirement_md_path: str = "",
        current_node_name: str = "",
        language: str = "python",
        overwrite: bool = True,
        temperature: float = 0.3,
        max_tokens: int = MAX_TOKENS,
        dependency_context: str | None = None,
        ancestor_session_state_context: str | None = None,
        additional_generation_context: str = "",
    ) -> Path:
        del requirement_md_path
        language_clean = language.strip().lower() if language else "python"
        target_path = Path(code_path)
        inferred_node_name = current_node_name.strip() if current_node_name else target_path.stem
        target_ext = self._language_extension(language_clean)
        if target_path.suffix.lower() != target_ext:
            target_path = target_path.with_suffix(target_ext)

        if not target_path.exists():
            raise FileNotFoundError(f"Code file not found: {target_path}")

        original_code = target_path.read_text(encoding="utf-8")
        contract_text = self.get_feedback_contract_text()

        user_prompt = self._build_amendment_prompt(
            original_code=original_code,
            amendment=amendment,
            language_clean=language_clean,
            contract_text=contract_text,
            additional_generation_context=additional_generation_context,
        )

        if dependency_context is None:
            dependency_names = _extract_declared_dependencies_from_code(original_code)
            dependency_context = _build_dependency_derived_context(
                node_dir=target_path.parent.resolve(),
                dependency_names=dependency_names,
            )
        if dependency_context:
            user_prompt += (
                "\n\nDependency derived context (authoritative):\n"
                f"{dependency_context}\n"
                "- Read only the listed dependency ids and derived keys; do not invent upstream fields.\n"
            )

        if ancestor_session_state_context is None:
            ancestor_session_state_context = _build_ancestor_session_state_context(
                graph_plan_path=graph_plan_path,
                node_name=inferred_node_name,
            )
        if ancestor_session_state_context:
            user_prompt += (
                "\n\nAncestor session_state context (authoritative):\n"
                f"{ancestor_session_state_context}\n"
                "- Read only the available session_state keys listed here; do not invent upstream keys.\n"
                "- New keys may be written when the amendment requires cross-step state.\n"
            )

        return self.code_to_file(
            user_prompt,
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def get_node_contract_text(self) -> str:
        raise NotImplementedError

    def get_feedback_contract_text(self) -> str:
        raise NotImplementedError


@dataclass
class WorkflowFileNodeCoder(PromptNodeFileCoderBase):
    def _build_requirement_prompt(
        self,
        node_name: str,
        node_meta: NodeMeta,
        requirement_text: str,
        node_markdown_reference: str,
        output_path: str,
        graph_plan_path: str,
        language_clean: str,
        node_contract_text: str,
        dependency_context: str | None = None,
        ancestor_session_state_context: str | None = None,
        root_dir_path: str = "",
        additional_generation_context: str = "",
    ) -> str:
        base_prompt = super()._build_requirement_prompt(
            node_name=node_name,
            node_meta=node_meta,
            requirement_text=requirement_text,
            node_markdown_reference=node_markdown_reference,
            output_path=output_path,
            graph_plan_path=graph_plan_path,
            language_clean=language_clean,
            node_contract_text=node_contract_text,
            dependency_context=dependency_context,
            ancestor_session_state_context=ancestor_session_state_context,
            root_dir_path=root_dir_path,
            additional_generation_context=additional_generation_context,
        )

        ext_data = node_meta.ext_data if isinstance(node_meta.ext_data, Mapping) else {}
        remote_desc = str(ext_data.get("remote_desc", "")).strip()

        if not remote_desc:
            return base_prompt

        return (
            base_prompt
            + "\n\nWorkflowFileNode remote persistence rule (authoritative):\n"
            + f"- remote_desc: {remote_desc}\n"
            + "- Implement the required remote persistence explicitly in process_input, "
            "using a small helper only when needed; no inherited storage method exists to override.\n"
            + "- Return the actual persisted location/identifier in derived only after storage succeeds; "
            "never fabricate persistence results.\n"
        )

    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowFileNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Generate a WorkflowFileNode subclass with STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            "Keep implementation minimal: only imports, constants, and methods required by this node contract.\n"
            f"Implement the abstract subclass hook {subclass_hook_text} with file_bytes: bytes and return StepRunOutput.\n"
            "Process a single upload's bytes according to the requirements; the base class neither decodes uploads nor persists data.\n"
            "Implement local or remote persistence explicitly in process_input only when required, and expose actual saved paths/identifiers in derived.\n"
            "Do not normalize file_path/base64/files/uploads in this subclass; the HTTP backend converts one upload to bytes before engine input.\n"
            "Define __init__ calling super().__init__() and self.setWaitFunc(self.wait_function). In wait_function, use param.condition and the can_run guard from the runtime policy.\n"
            "File readiness is self.STEP_ID in param.inputs and isinstance(param.inputs[self.STEP_ID], (bytes, bytearray)); empty bytes are valid unless requirements reject them.\n"
            "Report running/awaiting_input and return readiness without blocking; do not rely on input truthiness or parse legacy upload formats in the gate.\n"
            "Keep card payload JSON-serializable and derived payload structured for downstream nodes.\n"
            "Keep the inherited run orchestration.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        return (
            "Preserve the WorkflowFileNode bytes-processing and explicit input-gate contract below. "
            "Preserve or implement requirement-defined persistence, including ext_data.remote_desc, explicitly; "
            "never replace the hook with assumed inherited storage behavior.\n"
            + self.get_node_contract_text()
        )


@dataclass
class WorkflowStepNodeCoder(PromptNodeFileCoderBase):
    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowStepNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Generate a WorkflowStepNode subclass with STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            f"Implement business logic in {subclass_hook_text} and return StepRunOutput.\n"
            "If node metadata includes inputs_format, parse/validate user_input according to that schema (field names + primitive types).\n"
            "Set INPUT_REQUIRED = False when ext_data.type is not user_input and the step should run automatically.\n"
            "Keep implementation minimal: only imports, constants, and methods required by this node contract.\n"
            "Read upstream values from dependency_results[step_id].derived and persist cross-step values in session_state.\n"
            "When a required variable is absent in both dependency_results[step_id].derived and session_state, use safe fallback handling before returning explicit validation errors.\n"
            "Extract upstream variables only from nodes listed in DEPENDENCIES and from keys present in those dependencies' derived payloads.\n"
            "When dependency context is provided, treat it as authoritative for dependency ids and derived keys; do not invent non-existent upstream keys.\n"
            "Keep card payload JSON-serializable and derived payload structured for downstream nodes.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowStepNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Preserve the WorkflowStepNode contract "
            f"(STEP_ID/TITLE/PROMPT/DEPENDENCIES and {subclass_hook_text} returning StepRunOutput).\n"
        )


@dataclass
class RequestNodeFileCoder(PromptNodeFileCoderBase):
    """Generate any node kind using request metadata, never per-request instance fields."""

    __hash__ = object.__hash__

    def _writer_request(self) -> Mapping[str, Any]:
        try:
            state = self.get_workflow_param().state
        except RuntimeError:
            # Direct prompt-building calls can supply NodeMeta explicitly.
            return {}
        request = state.get("node_writer", {})
        return request if isinstance(request, Mapping) else {}

    def _request_node_meta(self) -> NodeMeta | None:
        request = self._writer_request()
        node_meta = request.get("node_meta")
        if isinstance(node_meta, Mapping):
            node_meta = NodeMeta.from_dict(node_meta)
        if isinstance(node_meta, NodeMeta):
            return node_meta
        graph_path = request.get("graph_plan_path")
        node_name = request.get("node_name") or Path(str(request.get("code_path", ""))).stem
        return Graph(graph_path).get_node_meta(node_name) if graph_path and node_name else None

    def _contract_coder(self, node_meta: NodeMeta | None) -> type[PromptNodeFileCoderBase]:
        reference = resolve_workflow_node_reference(
            meta_node_kind=node_meta.meta_node_kind or None if node_meta is not None else None,
            ext_data=node_meta.ext_data if node_meta is not None else None,
        )
        if reference.meta_node_kind == "WorkflowFileNode":
            return WorkflowFileNodeCoder
        return WorkflowStepNodeCoder

    def get_node_contract_text(self, node_meta: NodeMeta | None = None) -> str:
        coder = self._contract_coder(node_meta if node_meta is not None else self._request_node_meta())
        return coder.get_node_contract_text(self)

    def get_feedback_contract_text(self, node_meta: NodeMeta | None = None) -> str:
        coder = self._contract_coder(node_meta if node_meta is not None else self._request_node_meta())
        if coder is WorkflowFileNodeCoder:
            return (
                "Preserve the WorkflowFileNode bytes-processing and explicit input-gate contract below. "
                "Preserve or implement requirement-defined persistence, including ext_data.remote_desc, explicitly; "
                "never replace the hook with assumed inherited storage behavior.\n"
                + coder.get_node_contract_text(self)
            )
        return coder.get_feedback_contract_text(self)

    def _node_reference_context(self, node_meta: NodeMeta) -> str:
        ext_data = node_meta.ext_data if isinstance(node_meta.ext_data, Mapping) else {}
        coder = self._contract_coder(node_meta)
        if coder is WorkflowFileNodeCoder:
            remote_desc = str(ext_data.get("remote_desc", "")).strip()
            if not remote_desc:
                return ""
            return (
                "\n\nWorkflowFileNode remote persistence rule (authoritative):\n"
                f"- remote_desc: {remote_desc}\n"
                "- Implement the required remote persistence explicitly in process_input; "
                "no inherited storage method exists to override.\n"
                "- Return the actual persisted location/identifier in derived only after storage succeeds; "
                "never fabricate persistence results.\n"
            )
        return ""

    def _build_requirement_prompt(
        self,
        node_name: str,
        node_meta: NodeMeta,
        requirement_text: str,
        node_markdown_reference: str,
        output_path: str,
        graph_plan_path: str,
        language_clean: str,
        node_contract_text: str,
        dependency_context: str | None = None,
        ancestor_session_state_context: str | None = None,
        root_dir_path: str = "",
        additional_generation_context: str = "",
    ) -> str:
        # Only the base prompt builder is shared; specialized builders use
        # super() and cannot be called on this unrelated concrete subclass.
        del node_contract_text
        prompt = super()._build_requirement_prompt(
            node_name=node_name, node_meta=node_meta, requirement_text=requirement_text,
            node_markdown_reference=node_markdown_reference, output_path=output_path,
            graph_plan_path=graph_plan_path, language_clean=language_clean,
            node_contract_text=self.get_node_contract_text(node_meta),
            dependency_context=dependency_context,
            ancestor_session_state_context=ancestor_session_state_context,
            root_dir_path=root_dir_path, additional_generation_context=additional_generation_context,
        )
        return prompt + self._node_reference_context(node_meta)

    def _build_amendment_prompt(
        self,
        original_code: str,
        amendment: str,
        language_clean: str,
        contract_text: str,
        additional_generation_context: str = "",
    ) -> str:
        prompt = super()._build_amendment_prompt(
            original_code, amendment, language_clean, contract_text, additional_generation_context,
        )
        node_meta = self._request_node_meta()
        if node_meta is not None:
            prompt += self._node_reference_context(node_meta)
        return prompt