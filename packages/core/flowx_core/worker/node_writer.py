import json
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS
from flowx_core.context_builder.context import GraphContextBuilder
from flowx_core.architect.graph import Graph, NodeMeta
from flowx_core.tools.file_tools import compile_node_file_and_get_derived_keys
from flowx_core.tools.workflow_node_reference import (
    render_subclass_guidance_method_signatures,
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


def _resolve_named_root(
    configured_root_path: str,
    root_dir_path: str,
    default_dirname: str,
) -> Path:
    if configured_root_path:
        configured = Path(configured_root_path).expanduser().resolve()
        if configured.is_dir():
            return configured

    if root_dir_path:
        root_dir = Path(root_dir_path).expanduser().resolve()
        direct = root_dir / default_dirname
        if direct.is_dir():
            return direct
        parent = root_dir.parent / default_dirname
        if parent.is_dir():
            return parent

    return ROOT_DIR.parent / default_dirname


def _read_named_markdown(root_dir: Path, item_name: str, markdown_name: str) -> str:
    if not item_name:
        return ""

    doc_path = root_dir / item_name / markdown_name
    if not doc_path.is_file():
        return ""
    return doc_path.read_text(encoding="utf-8").strip()


def _parse_markdown_sections(markdown_text: str) -> dict[str, str]:
    if not markdown_text.strip():
        return {}

    try:
        from flowx_core.tools.file_tools import parse_skill_md

        parsed = parse_skill_md(markdown_text)
    except Exception:
        return {}

    return {str(key).strip(): str(value).strip() for key, value in parsed.items()}


def _reference_method_signature_text(reference, method_names: tuple[str, ...]) -> str:
    signatures = render_workflow_method_signatures(reference.base_class, method_names)
    return ", ".join(signatures) if signatures else "none"


def _reference_primary_method_signature(reference, method_names: tuple[str, ...], fallback: str) -> str:
    signatures = render_workflow_method_signatures(reference.base_class, method_names)
    return signatures[0] if signatures else fallback


def _reference_guidance_method_signature_text(reference) -> str:
    signatures = render_subclass_guidance_method_signatures(
        reference.base_class,
        reference.subclass_implementation_methods,
    )
    return ", ".join(signatures) if signatures else "none"
    

@dataclass
class PromptNodeFileCoderBase(Coder):
    prompt_path: str = "worker/prompts/pydaograph_node_prompt.md"
    root_dir_path: str = ""
    context_text: str = ""
    ancestor_session_state_context_text: str = ""

    def __post_init__(self) -> None:
        prompt_file = ROOT_DIR / self.prompt_path
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        workflow_step_meta_catalog_text = render_workflow_step_meta_catalog()
        self.system_prompt = (
            f"{prompt_file.read_text(encoding='utf-8')}\n\n"
            "## ag_ui_workflow Base Step Metas (Authoritative)\n"
            "Use the injected ag_ui_workflow base-node step_meta()/meta_node_kind() catalog below as the only source of truth for node contracts.\n\n"
            f"{workflow_step_meta_catalog_text}\n"
        )
        super().__post_init__()

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
    ) -> str:
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
        if ext_type in ("user_input", "skill") and inputs_format_text != "none":
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

        self.context_text = ""
        self.ancestor_session_state_context_text = ""

        dependency_context = _build_dependency_derived_context(
            node_dir=Path(output_path).expanduser().resolve().parent,
            dependency_names=list(node_meta.depends or []),
        )
        if dependency_context:
            user_prompt += (
                "\n\nDependency derived keys from existing dependency files "
                "(parsed via compile_node_file_and_get_derived_keys, authoritative):\n"
                f"{dependency_context}\n"
                "- Strict rule: any key read from dependency_results[dep].derived must come only from the keys listed in this dependency_context.\n"
                "- Never access or invent dependency derived keys outside this dependency_context.\n"
            )

        ancestor_session_state_context = _build_ancestor_session_state_context(
            graph_plan_path=graph_plan_path,
            node_name=node_name,
        )
        self.ancestor_session_state_context_text = ancestor_session_state_context
        if ancestor_session_state_context:
            user_prompt += (
                "\n\nAncestor session_state keys from node files "
                "(via get_ancestor_session_state_keys, authoritative):\n"
                f"{ancestor_session_state_context}\n"
                "- Strict rule: any key read/write in session_state must come only from this ancestor_session_state_context.\n"
                "- Never read/write or invent session_state keys outside this ancestor_session_state_context.\n"
                "- For missing allowed keys, use safe fallback handling and explicit validation errors when still unresolved.\n"
            )

        if graph_plan_path:
            context_builder = GraphContextBuilder(root_path=self.root_dir_path, language=language_clean)
            context_builder.search(current_node_name=node_name, graph_plan_path=graph_plan_path)

            context_text = context_builder.build(limit=5)
            self.context_text = context_text or ""
            if context_text:
                user_prompt += (
                    "\n\nDependency context from GraphContextBuilder (authoritative):\n"
                    "- Infer each dependency node's STEP_ID and available derived keys from this context.\n"
                    "- Prefer using the context node's derived key-values as the first-choice upstream fields.\n"
                    f"- In {selected_subclass_hook}, extract upstream variables from dependency_results using only those dependency STEP_IDs and derived keys.\n"
                    "- First resolve required values from dependency_results/session_state; if still unresolved, use safe fallback handling and then validate/fail clearly when still missing.\n"
                    "- Avoid guessed upstream field names; if a needed key is uncertain, use safe fallback handling without TODO markers.\n\n"
                    f"{context_text}"
                )

        if user_prompt.strip():
            user_prompt += (
                f"\n\nReturn only runnable {language_clean} code for this node."
                " Do not include markdown fences or explanation text."
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
        )

        dependency_names = _extract_declared_dependencies_from_code(original_code)
        dependency_context = _build_dependency_derived_context(
            node_dir=target_path.parent.resolve(),
            dependency_names=dependency_names,
        )
        if dependency_context:
            user_prompt += (
                "\n\nDependency derived keys from existing dependency files "
                "(parsed via compile_node_file_and_get_derived_keys, authoritative):\n"
                f"{dependency_context}\n"
            )

        if graph_plan_path:
            context_builder = GraphContextBuilder(root_path=self.root_dir_path, language=language_clean)
            context_builder.search(current_node_name=inferred_node_name, graph_plan_path=graph_plan_path)
            self.context_text = context_builder.build(limit=5) or ""

        if self.context_text.strip():
            user_prompt += (
                "\n\nDependency context from GraphContextBuilder (authoritative):\n"
                "- Infer each dependency node's STEP_ID and available derived keys from this context.\n"
                "- Prefer using the context node's derived key-values as the first-choice upstream fields.\n"
                "- In the selected subclass hook or inherited runtime path, extract upstream variables from dependency_results using only those dependency STEP_IDs and derived keys.\n"
                "- Strict rule: node's get keys from derived must strictly come from dependency_context; do not access derived keys outside dependency_context.\n"
                "- First resolve required values from dependency_results/session_state; if still unresolved, use safe fallback handling and then validate/fail clearly when still missing.\n"
                "- Avoid guessed upstream field names; if a needed key is uncertain, use safe fallback handling without TODO markers.\n\n"
                f"{self.context_text}\n"
            )

        if graph_plan_path:
            self.ancestor_session_state_context_text = _build_ancestor_session_state_context(
                graph_plan_path=graph_plan_path,
                node_name=inferred_node_name,
            )

        if self.ancestor_session_state_context_text.strip():
            user_prompt += (
                "\n\nAncestor session_state keys from node files "
                "(via get_ancestor_session_state_keys, authoritative):\n"
                f"{self.ancestor_session_state_context_text}\n"
                "- Strict rule: node's get keys from session_state must strictly come from ancestor_session_state_context.\n"
                "- Never read/write session_state keys outside ancestor_session_state_context unless amendment explicitly adds and justifies a new key.\n"
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
class WorkflowOperationNodeCoder(PromptNodeFileCoderBase):
    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowOperationNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Generate a WorkflowOperationNode subclass with STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            f"Implement business logic in {subclass_hook_text} and return StepRunOutput.\n"
            "Keep implementation minimal: only imports, constants, and methods required by this node contract.\n"
            "Read upstream values from dependency_results[step_id].derived and persist cross-step values in session_state.\n"
            "When a required variable is absent in both dependency_results[step_id].derived and session_state, use safe fallback handling before returning explicit validation errors.\n"
            "Extract upstream variables only from nodes listed in DEPENDENCIES and from keys present in those dependencies' derived payloads.\n"
            "When dependency context is provided, treat it as authoritative for dependency ids and derived keys; do not invent non-existent upstream keys.\n"
            "Keep card payload JSON-serializable and derived payload structured for downstream nodes.\n"
            "Do not require user input for this node.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowOperationNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Preserve the WorkflowOperationNode contract "
            f"(STEP_ID/TITLE/PROMPT/DEPENDENCIES and {subclass_hook_text} returning StepRunOutput).\n"
        )


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
        )

        ext_data = node_meta.ext_data if isinstance(node_meta.ext_data, Mapping) else {}
        remote_desc = ""
        if isinstance(ext_data, Mapping):
            remote_desc = str(ext_data.get("remote_desc", "")).strip()

        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowFileNode")
        main_utility_text = _reference_method_signature_text(reference, reference.main_utility_methods)
        step_output_text = _reference_method_signature_text(reference, reference.step_output_schema_methods)

        if not remote_desc:
            return (
                base_prompt
                + "\n\nWorkflowFileNode generation rule (authoritative):\n"
                + "- Do not implement any custom methods.\n"
                + "- The selected WorkflowFileNode base reference exposes no decorator-marked subclass implementation hook.\n"
                + f"- Rely on the inherited main utility {main_utility_text} and StepRunOutput contract {step_output_text}.\n"
                + "- Only define required class constants (STEP_ID, TITLE, PROMPT, DEPENDENCIES).\n"
            )

        return (
            base_prompt
            + "\n\nWorkflowFileNode remote persistence rule (authoritative):\n"
            + "- ext_data.remote_desc is provided; add only the smallest custom remote-persistence override truly required by it.\n"
            + f"- remote_desc: {remote_desc}\n"
            + f"- Inherited main utility: {main_utility_text}\n"
            + f"- Inherited StepRunOutput contract method: {step_output_text}\n"
            + "- Do not modify or override other base persistence/output methods unless strictly required by remote_desc.\n"
        )

    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowFileNode")
        main_utility_text = _reference_method_signature_text(reference, reference.main_utility_methods)
        step_output_text = _reference_method_signature_text(reference, reference.step_output_schema_methods)
        return (
            "Generate a WorkflowFileNode subclass with STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            "Keep implementation minimal: only imports, constants, and methods required by this node contract.\n"
            "By default, do not implement any custom methods; the selected WorkflowFileNode base reference exposes no decorator-marked subclass hook for this node kind.\n"
            f"Rely on inherited main utility {main_utility_text} and StepRunOutput contract {step_output_text}.\n"
            "Only when ext_data.remote_desc is present and truly requires custom remote persistence, add the smallest override needed for that behavior.\n"
            "saved_files contains uploaded files already persisted by the inherited WorkflowFileNode storage path, including original fileName(fileName) and saved location(path).\n"
            "Keep card payload JSON-serializable and derived payload structured for downstream nodes.\n"
            "Ensure this node expects user file input and supports multiple uploaded files.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        return (
            "Preserve the WorkflowFileNode contract "
            "(STEP_ID/TITLE/PROMPT/DEPENDENCIES). "
            "Default to no custom methods; only add the smallest remote-persistence override when remote storage behavior is explicitly required (for example ext_data.remote_desc).\n"
        )


@dataclass
class SpatialTemporalContractNodeCoder(PromptNodeFileCoderBase):
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
        )

        reference = resolve_workflow_node_reference(meta_node_kind="SpatialTemporalContractNode")
        step_output_text = _reference_method_signature_text(reference, reference.step_output_schema_methods)
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        guidance_hook_text = _reference_guidance_method_signature_text(reference)

        if guidance_hook_text == "none":
            guidance_rule = (
                f"- The base SpatialTemporalContractNode does not consume subclass PROMPT during model generation; if this node's desc/ext_data.desc/PROMPT carries business guidance, preserve the inherited generation flow and prefer the smallest prompt/guidance helper reachable from {subclass_hook_text} when one exists.\n"
            )
        else:
            guidance_rule = (
                f"- The base SpatialTemporalContractNode does not consume subclass PROMPT during model generation; if this node's desc/ext_data.desc/PROMPT carries business guidance, inject it through the smallest parsed prompt/guidance helper reachable from {subclass_hook_text}: {guidance_hook_text}. Preserve the inherited generation flow.\n"
            )

        return (
            base_prompt
            + "\n\nSpatialTemporalContractNode runtime contract (authoritative):\n"
            + "- Subclass SpatialTemporalContractNode.\n"
            + "- Define STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            + f"- Keep implementation minimal: preserve the inherited run/process_operation flow and StepRunOutput contract {step_output_text}, always add clone(self) -> self, and add only the smallest guidance override needed for this node.\n"
            + guidance_rule
            + f"- Reserve overriding {subclass_hook_text} for cases that truly need custom model invocation or contract parsing beyond prompt-guidance changes.\n"
            + f"- Do not replace {step_output_text}, run, or __init__ unless the requirement explicitly demands a different runtime contract.\n"
            + "- Ensure the contract description comes from session_state['spatialTemporalContractDescription'] or an upstream dependency output/card.\n"
            + "- The base class returns StepRunOutput with derived keys such as spatialTemporalContract, spatialTemporalContractJson, objectCount, relationCount, model, rawResponse, and optional usage.\n"
            + "- This node is non-interactive; do not introduce direct user input handling or inputs_format parsing.\n"
        )

    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="SpatialTemporalContractNode")
        step_output_text = _reference_method_signature_text(reference, reference.step_output_schema_methods)
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        guidance_hook_text = _reference_guidance_method_signature_text(reference)
        if guidance_hook_text == "none":
            guidance_rule = (
                f"If node-specific contract semantics must influence the generated contract, prefer the smallest prompt/guidance helper reachable from {subclass_hook_text} when one exists, while preserving the inherited run/process_operation flow and JSON parsing path.\n"
            )
        else:
            guidance_rule = (
                f"If node-specific contract semantics must influence the generated contract, prefer overriding the smallest parsed prompt/guidance helper reachable from {subclass_hook_text}: {guidance_hook_text}, while preserving the inherited run/process_operation flow and JSON parsing path.\n"
            )
        return (
            "Generate a SpatialTemporalContractNode subclass with STEP_ID, TITLE, PROMPT, and DEPENDENCIES.\n"
            "Keep implementation minimal: only imports, class constants, clone(self), and the smallest guidance override needed to make this node's own desc/PROMPT affect contract generation.\n"
            "The base SpatialTemporalContractNode does not use subclass PROMPT directly during model generation.\n"
            + guidance_rule
            + f"Reserve {subclass_hook_text} for cases that truly need custom model invocation instead of prompt-guidance changes.\n"
            f"Do NOT replace inherited runtime methods such as {step_output_text}, run, or __init__ unless the requirement explicitly asks to customize the base contract-generation behavior.\n"
            "This node is non-interactive and should not parse direct user input or define inputs_format.\n"
            "Ensure upstream dependencies or session_state provide spatialTemporalContractDescription or equivalent descriptive text for the base class to consume.\n"
            "The inherited base implementation will resolve the description, call the configured model, and return StepRunOutput with derived keys including spatialTemporalContract, spatialTemporalContractJson, objectCount, relationCount, model, rawResponse, and optional usage.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="SpatialTemporalContractNode")
        step_output_text = _reference_method_signature_text(reference, reference.step_output_schema_methods)
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        guidance_hook_text = _reference_guidance_method_signature_text(reference)
        if guidance_hook_text == "none":
            guidance_rule = (
                f"If the node needs its own generation guidance, prefer the smallest prompt/guidance helper reachable from {subclass_hook_text} when one exists."
            )
        else:
            guidance_rule = (
                f"If the node needs its own generation guidance, prefer the smallest parsed prompt/guidance helper reachable from {subclass_hook_text}: {guidance_hook_text}."
            )
        return (
            "Preserve the SpatialTemporalContractNode contract "
            "(STEP_ID/TITLE/PROMPT/DEPENDENCIES plus clone returning self). "
            f"Keep the inherited run/{step_output_text} flow. {guidance_rule} Reserve custom {subclass_hook_text} overrides for true model-invocation changes.\n"
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
class WorkflowSkillNodeCoder(PromptNodeFileCoderBase):
    default_skills_dirname: str = "skills"
    skills_root_path: str = ""

    def _default_skills_root(self) -> Path:
        return _resolve_named_root(
            configured_root_path=self.skills_root_path,
            root_dir_path=self.root_dir_path,
            default_dirname=self.default_skills_dirname,
        )

    def _extract_skill_name(self, node_meta: NodeMeta) -> str:
        ext_data = node_meta.ext_data
        if isinstance(ext_data, Mapping):
            return str(ext_data.get("skill_name", "")).strip()
        return ""

    def _read_skill_markdown(self, skills_root: Path, skill_name: str) -> str:
        return _read_named_markdown(skills_root, skill_name, "skill.md")

    def _extract_skill_sections(self, skill_markdown: str) -> tuple[str, str]:
        """Return (using_section, examples_section) from a parsed skill.md."""
        sections = _parse_markdown_sections(skill_markdown)
        return sections.get("Using", "").strip(), sections.get("Examples", "").strip()

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
        )

        skills_root = self._default_skills_root()
        skill_name = self._extract_skill_name(node_meta)
        skill_markdown = self._read_skill_markdown(skills_root, skill_name)
        skill_using, skill_examples = self._extract_skill_sections(skill_markdown)
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowSkillNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)

        skill_context_lines = [
            "",
            "Skill node context (authoritative):",
            f"- skills_root: {skills_root}",
            f"- selected_skill_name: {skill_name or 'none'}",
            f"- SKILL_DIR class constant must be set to: {str(skills_root / skill_name) if skill_name else 'path/to/skill/dir'}",
        ]

        if skill_using:
            skill_context_lines.extend(
                [
                    "",
                    "skill.md ## Using section (authoritative — tells you how to invoke the skill):",
                    skill_using,
                ]
            )

        if skill_examples:
            skill_context_lines.extend(
                [
                    "",
                    "skill.md ## Examples section (authoritative — concrete usage patterns):",
                    skill_examples,
                ]
            )

        skill_context_lines.extend(
            [
                "",
                "Implementation constraints for this skill node:",
                "- Subclass WorkflowSkillNode.",
                "- Set SKILL_DIR to the exact skill directory path shown above.",
                "- Set SKILL_MD_PATH = str(Path(SKILL_DIR) / 'skill.md') so the base class parses the skill doc on init.",
                f"- Implement {subclass_hook_text} -> StepRunOutput.",
                f"- In {subclass_hook_text}, invoke the skill using the pattern described in the ## Using section; use self.skill_using and self.skill_examples for inline reference if needed.",
                "- Use user_input when present; keep robust fallback behavior if user_input is empty.",
                "- Read upstream values from dependency_results[step_id].derived as needed.",
                "- Do not hardcode skill logic that contradicts skill.md ## Using; follow it exactly.",
                "- Keep output JSON-serializable and include useful derived fields for downstream nodes.",
            ]
        )

        return base_prompt + "\n" + "\n".join(skill_context_lines)

    def get_node_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowSkillNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Generate a WorkflowSkillNode subclass with STEP_ID, TITLE, PROMPT, DEPENDENCIES, SKILL_DIR, and SKILL_MD_PATH.\n"
            "Set SKILL_DIR to the absolute path of the chosen skill directory.\n"
            "Set SKILL_MD_PATH = str(Path(SKILL_DIR) / 'skill.md') — the base class reads and parses it on __init__.\n"
            "After __init__, self.skill_description, self.skill_using, self.skill_examples are available as strings parsed from skill.md.\n"
            f"Implement skill invocation in {subclass_hook_text} and return StepRunOutput.\n"
            "Use user_input when meaningful for the skill behavior; tolerate empty user_input when the step does not require it.\n"
            "Invoke the skill according to the ## Using section of skill.md; do not invent an invocation pattern.\n"
            "Keep implementation minimal: only imports, constants, and methods required by this node contract.\n"
            "Read upstream values from dependency_results[step_id].derived and persist cross-step values in session_state when needed.\n"
            "Extract upstream variables only from nodes listed in DEPENDENCIES and from keys present in those dependencies' derived payloads.\n"
            "When dependency context is provided, treat it as authoritative for dependency ids and derived keys; do not invent non-existent upstream keys.\n"
            "Keep card payload JSON-serializable and derived payload structured for downstream nodes.\n"
            "This node can consume direct user input when the workflow step is configured with inputRequired=true.\n\n"
        )

    def get_feedback_contract_text(self) -> str:
        reference = resolve_workflow_node_reference(meta_node_kind="WorkflowSkillNode")
        subclass_hook_text = _reference_method_signature_text(reference, reference.subclass_implementation_methods)
        return (
            "Preserve the WorkflowSkillNode contract "
            f"(STEP_ID/TITLE/PROMPT/DEPENDENCIES/SKILL_DIR/SKILL_MD_PATH and {subclass_hook_text} returning StepRunOutput).\n"
        )