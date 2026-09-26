from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS
from flowx_core.tools.file_tools import (
    collect_session_state_keys_from_node_file,
    compile_node_file_and_get_derived_keys,
    compile_node_file_and_get_step_output_card_schema,
)
from flowx_core.tools.workflow_node_reference import (
    render_workflow_node_reference,
    render_workflow_step_meta_catalog,
    resolve_workflow_node_reference,
)


_WORKFLOW_BASE_CLASS_NAMES = frozenset(
    {
        "WorkflowStepNode",
        "WorkflowOperationNode",
        "WorkflowFileNode",
        "WorkflowSkillNode",
    }
)


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _extract_class_string_constant(cls: ast.ClassDef, attr_name: str) -> str | None:
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == attr_name:
                if isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    return stmt.value.value
    return None


def _parse_workflow_node_class(source: str) -> dict[str, Any] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue

        base_names = []
        for base in node.bases:
            base_name = _base_name(base)
            if base_name in _WORKFLOW_BASE_CLASS_NAMES and base_name not in base_names:
                base_names.append(base_name)
        if not base_names:
            continue

        declared_methods = [
            stmt.name
            for stmt in node.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        return {
            "class_name": node.name,
            "meta_node_kind": base_names[0],
            "step_id": _extract_class_string_constant(node, "STEP_ID") or node.name,
            "title": _extract_class_string_constant(node, "TITLE") or node.name,
            "prompt": _extract_class_string_constant(node, "PROMPT") or "",
            "declared_methods": declared_methods,
        }
    return None


@dataclass
class PromptNodeTestFileCoder(Coder):
    prompt_path: str = "worker/prompts/pydaograph_node_test_prompt.md"
    root_dir_path: str = ""

    def __post_init__(self) -> None:
        prompt_file = ROOT_DIR / self.prompt_path
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        workflow_step_meta_catalog_text = render_workflow_step_meta_catalog()
        self.system_prompt = (
            f"{prompt_file.read_text(encoding='utf-8')}\n\n"
            "## ag_ui_workflow Base Step Metas (Authoritative)\n"
            "Use the injected ag_ui_workflow base-node step_meta()/meta_node_kind() catalog below as the only source of truth for node runtime contracts.\n\n"
            f"{workflow_step_meta_catalog_text}\n"
        )
        super().__post_init__()

    def _build_user_prompt(self, node_file_path: str) -> str:
        path = Path(node_file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Node file not found: {path}")

        source = path.read_text(encoding="utf-8")
        node_context = _parse_workflow_node_class(source)
        if node_context is None:
            raise ValueError(f"No ag_ui_workflow node subclass found in file: {path}")

        reference = resolve_workflow_node_reference(
            meta_node_kind=node_context["meta_node_kind"],
        )
        session_state_keys = sorted(
            collect_session_state_keys_from_node_file(str(path), node_context["class_name"])
        )
        derived_keys = compile_node_file_and_get_derived_keys(str(path))
        card_schema = compile_node_file_and_get_step_output_card_schema(str(path))
        test_loader_expr = f"Path(__file__).resolve().parents[1] / {path.name!r}"

        return (
            "Generate a single-node pytest module for this AG-UI workflow node.\n"
            f"Node file name: {path.name}\n"
            f"Node class name: {node_context['class_name']}\n"
            f"STEP_ID: {node_context['step_id']}\n"
            f"Detected workflow base class: {node_context['meta_node_kind']}\n"
            f"Declared methods in node file: {', '.join(node_context['declared_methods']) if node_context['declared_methods'] else 'none'}\n"
            f"Recommended node file loader expression inside the test: {test_loader_expr}\n\n"
            "Single-node test contract (authoritative):\n"
            "- Base the tests only on this node code file and the injected ag_ui_workflow base-class contract.\n"
            "- Do not read workflow.json, requirement markdown, node_docs, or other generated node files.\n"
            "- Use pytest.\n"
            "- Import the node module directly from the node file path with importlib.util.spec_from_file_location so the test stays stable even when the generated package layout is minimal.\n"
            "- If the module may import sibling files, prepend the node directory to sys.path before executing the module spec.\n"
            "- Keep the test deterministic and offline.\n"
            "- Prefer exercising the node's own subclass hook or overridden StepRunOutput method instead of the full workflow engine.\n"
            "- Create only the smallest dependency_results, session_state, user_input, saved_files, temp files, env vars, or monkeypatches that are implied by this node code.\n"
            "- When the node would otherwise call external services, install packages, spawn processes, or hit a network/LLM API, monkeypatch only that external boundary while still running the node's internal logic.\n"
            "- Assert concrete StepRunOutput.card and StepRunOutput.derived values or keys that are deterministically produced by this node code.\n"
            "- Include one identity test that covers importability plus STEP_ID and step_meta basics.\n"
            "- Include at least one behavioral test focused on the node's primary StepRunOutput contract.\n"
            "- For WorkflowFileNode, avoid duplicating ag_ui_workflow base-class tests; only exercise behavior that this node file defines or overrides.\n"
            "- For WorkflowSkillNode, avoid real installation side effects; monkeypatch installation behavior or create the smallest temporary skill.md needed to instantiate the class.\n"
            "- Return only runnable Python test code with no markdown fences or commentary.\n\n"
            "Selected ag_ui_workflow base-node reference (authoritative):\n"
            f"{render_workflow_node_reference(reference)}\n\n"
            "Static hints extracted from this node code file:\n"
            f"- detected session_state keys: {json.dumps(session_state_keys, ensure_ascii=False)}\n"
            f"- detected derived output keys: {json.dumps(derived_keys, ensure_ascii=False)}\n"
            f"- detected StepRunOutput card preview: {json.dumps(card_schema.get('card') if card_schema else None, ensure_ascii=False, indent=2)}\n\n"
            "Node code file (authoritative):\n"
            f"```python\n{source}\n```\n"
        )

    def write_test_from_node_file(
        self,
        node_file_path: str,
        output_path: str,
        *,
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        target_path = Path(output_path)
        if target_path.suffix.lower() != ".py":
            target_path = target_path.with_suffix(".py")

        return self.code_to_file(
            self._build_user_prompt(node_file_path),
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )