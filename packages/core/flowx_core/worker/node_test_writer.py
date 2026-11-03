# Modified 2026-10-13: bytes-hook behavioral tests and configured concrete-node prompts.
# Modified: apply scoped instance settings before prompt-file use and composition.
# Modified 2026-11-01: generated tests construct native-derived nodes with Class(), never object.__new__.
# Modified 2026-11-02: identify step metadata as a dictionary in test write/amend prompts.
# Modified 2026-11-02: preserve Unicode offsets and multiline native-node test formatting.
from __future__ import annotations

import ast
import io
import json
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ag_ui_workflow import StepRunOutput

from flowx_core._paths import bootstrap_package_root


ROOT_DIR = bootstrap_package_root(__file__)

from flowx_core.llm_client.coder import Coder, MAX_TOKENS
from flowx_core.tools._node_settings import apply_node_settings
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
        "WorkflowFileNode",
    }
)

# Node classes derive from the native ``pydaograph.GNode`` runtime, so CPython
# rejects ``object.__new__(cls)`` with "TypeError: object.__new__(X) is not safe".
_NATIVE_INSTANTIATION_CONTRACT = (
    "- Instantiate the node with `NodeClass()` (or `NodeClass.__new__(NodeClass)`); "
    "never call `object.__new__(NodeClass)`, because node classes derive from the native "
    "`pydaograph.GNode` runtime and that raises "
    "`TypeError: object.__new__(X) is not safe, use X.__new__()`.\n"
)


def _is_dotted_name(node: ast.AST) -> bool:
    """True for a bare name or a ``Name.attr.attr`` chain (no calls or subscripts)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _character_offset(text: str, lineno: int | None, column: int | None) -> int:
    """Convert an AST (line, UTF-8 byte column) pair into a ``str`` index."""
    if lineno is None or column is None or lineno < 1:
        return 0
    # Python's parser counts only CR, LF and CRLF as physical line breaks;
    # str.splitlines() would also split Unicode separators inside literals.
    lines = list(io.StringIO(text, newline=""))
    if lineno > len(lines):
        return len(text)
    prefix = sum(len(line) for line in lines[: lineno - 1])
    encoded = lines[lineno - 1].encode("utf-8")[:column]
    return prefix + len(encoded.decode("utf-8", "ignore"))


def _native_instantiation_replacement(source: str, argument_source: str) -> str:
    """Replace a recognized call while retaining comments and needed parentheses."""
    # Keep the existing compact formatting for ordinary calls. A hash can only
    # be a comment here: AST recognition excludes string-valued expressions.
    if "#" not in source:
        replacement = f"{argument_source}()"
        try:
            ast.parse(replacement, mode="eval")
        except SyntaxError:
            pass
        else:
            return replacement

    # A wrapper keeps tokenization valid even when the call itself relies on an
    # enclosing expression for multiline continuation. Token columns count
    # characters; the source lines retain their original physical newlines.
    wrapped = "(\n" + source + "\n)"
    lines = list(io.StringIO(wrapped, newline=""))
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    tokens = list(tokenize.generate_tokens(io.StringIO(wrapped, newline=None).readline))
    tokens = [token for token in tokens if 2 <= token.start[0] <= len(lines) - 1]

    def offset(position: tuple[int, int]) -> int:
        line, column = position
        return offsets[line - 1] + column - 2  # Skip the synthetic "(\n".

    # The last parenthesis opens the call; earlier ones only group its callee.
    depth = 0
    opening = None
    trailing_comma = None
    for token in tokens:
        if token.type != tokenize.OP:
            continue
        if token.string == "(":
            if depth == 0:
                opening = token
            depth += 1
        elif token.string == ")":
            depth -= 1
        elif token.string == "," and depth == 1:
            trailing_comma = token

    if opening is None:
        raise ValueError("recognized call has no opening parenthesis")
    opening_offset = offset(opening.start)
    comma_offset = offset(trailing_comma.start) if trailing_comma is not None else None
    comments = [token for token in tokens if token.type == tokenize.COMMENT]

    # The original call parentheses provide implicit continuation and keep
    # comments and whitespace around the argument. Remove a trailing comma so
    # the parenthesized argument does not become a one-element tuple.
    body = source[opening_offset:]
    if comma_offset is not None:
        comma_in_body = comma_offset - opening_offset
        body = body[:comma_in_body] + body[comma_in_body + 1 :]

    callee_comments = [token for token in comments if offset(token.start) < opening_offset]
    if not callee_comments:
        return f"{body}()"

    # Comments between tokens in a multiline callee cannot remain outside an
    # expression. Place them inside an extra grouping without moving or
    # reformatting their physical lines.
    prefix = source[:opening_offset]
    preserved = []
    position = 0
    for token in tokens:
        start = offset(token.start)
        if start >= opening_offset:
            break
        end = offset(token.end)
        if token.type in (tokenize.NAME, tokenize.OP):
            preserved.extend((prefix[position:start], " " * (end - start)))
            position = end
    preserved.append(prefix[position:])
    return "(" + "".join(preserved) + body + ")()"


def normalize_native_node_instantiation(path: str | Path) -> int:
    """Rewrite ``object.__new__(X)`` into ``X()`` in a generated node test.

    Generated nodes inherit the native ``pydaograph.GNode`` runtime, where
    ``object.__new__(cls)`` raises ``TypeError: object.__new__(X) is not safe,
    use X.__new__()`` before any assertion runs. Only bare names and attribute
    chains are rewritten, so expressions with calls or subscripts keep their
    side effects untouched. Returns the number of rewritten call sites.
    """
    target_path = Path(path)
    try:
        with target_path.open(encoding="utf-8", newline="") as source_file:
            text = source_file.read()
        tree = ast.parse(text)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return 0
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "__new__"
            and isinstance(func.value, ast.Name)
            and func.value.id == "object"
            and len(node.args) == 1
            and not node.keywords
        ):
            continue
        argument = node.args[0]
        if not _is_dotted_name(argument):
            continue
        argument_start = _character_offset(text, argument.lineno, argument.col_offset)
        argument_end = _character_offset(text, argument.end_lineno, argument.end_col_offset)
        start = _character_offset(text, node.lineno, node.col_offset)
        end = _character_offset(text, node.end_lineno, node.end_col_offset)
        replacement = _native_instantiation_replacement(
            text[start:end], text[argument_start:argument_end]
        )
        edits.append((start, end, replacement))
    if not edits:
        return 0
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    compile(text, str(target_path), "exec")
    with target_path.open("w", encoding="utf-8", newline="") as source_file:
        source_file.write(text)
    return len(edits)


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
                "Use the injected ag_ui_workflow base-node step_meta()/meta_node_kind() catalog below as the only source of truth for node runtime contracts.\n\n"
                f"{workflow_step_meta_catalog_text}\n"
            )
        super().__post_init__()

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        """Write or amend a node test using a session-state request."""
        del user_input, dependency_results
        request = session_state.get("node_test_writer")
        if not isinstance(request, Mapping):
            raise ValueError("session_state['node_test_writer'] must be a write or amend request")

        action_key = f"{type(self).__name__}::action"
        action = session_state.get(action_key)
        if action not in ("write", "amend"):
            raise ValueError(f"{action_key} must be 'write' or 'amend'")

        node_file_path = request.get("node_file_path")
        output_path = request.get("output_path")
        if not isinstance(node_file_path, str) or not node_file_path.strip():
            raise ValueError("node_test_writer node_file_path must be a non-empty string")
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("node_test_writer output_path must be a non-empty string")

        options = {
            "overwrite": request.get("overwrite", True),
            "temperature": request.get("temperature", 0.2),
            "max_tokens": request.get("max_tokens", MAX_TOKENS),
        }
        if action == "write":
            written = self.write_test_from_node_file(node_file_path, output_path, **options)
        else:
            amendment = request.get("amendment")
            if not isinstance(amendment, str) or not amendment.strip():
                raise ValueError("amend requires a non-empty amendment")
            written = self.amend_test_with_feedback(node_file_path, output_path, amendment, **options)

        return StepRunOutput(derived={"test_file_path": str(written)})

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
            f"{_NATIVE_INSTANTIATION_CONTRACT}"
            "- Keep the test deterministic and offline.\n"
            "- Prefer exercising the node's own subclass hook or overridden StepRunOutput method instead of the full workflow engine.\n"
            "- Create only the smallest dependency_results, session_state, user_input, file_bytes, temp files, env vars, or monkeypatches that are implied by this node code.\n"
            "- When the node would otherwise call external services, install packages, spawn processes, or hit a network/LLM API, monkeypatch only that external boundary while still running the node's internal logic.\n"
            "- Assert concrete StepRunOutput.card and StepRunOutput.derived values or keys that are deterministically produced by this node code.\n"
            "- Include one identity test that covers importability plus STEP_ID and step_meta basics. "
            "`StepMeta` is a dict at runtime: assert `meta[\"id\"] == node.STEP_ID` and "
            "`meta[\"dependencies\"] == list(node.DEPENDENCIES)`; never use `meta.id` or `meta.dependencies`.\n"
            "- Include at least one behavioral test focused on the node's primary StepRunOutput contract.\n"
            "- For WorkflowFileNode, call process_input with one bytes payload (including empty bytes when allowed), and assert the subclass's real processing/persistence output. Do not assume inherited saved_files or upload decoding; avoid duplicating base-class tests.\n"
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

        written = self.code_to_file(
            self._build_user_prompt(node_file_path),
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        normalize_native_node_instantiation(written)
        return Path(written)

    def amend_test_with_feedback(
        self,
        node_file_path: str,
        output_path: str,
        amendment: str,
        *,
        overwrite: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS,
    ) -> Path:
        """Revise an existing test against the current node implementation."""
        target_path = Path(output_path)
        if target_path.suffix.lower() != ".py":
            target_path = target_path.with_suffix(".py")
        if not target_path.is_file():
            raise FileNotFoundError(f"Test file not found: {target_path}")

        prompt = (
            f"{self._build_user_prompt(node_file_path)}\n\n"
            "Amend the existing pytest module below using the feedback. "
            "Preserve valid tests and return only the complete updated Python test code.\n"
            f"Existing test code:\n{target_path.read_text(encoding='utf-8')}\n\n"
            f"Feedback:\n{amendment}\n"
        )
        written = self.code_to_file(
            prompt,
            str(target_path),
            overwrite=overwrite,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        normalize_native_node_instantiation(written)
        return Path(written)