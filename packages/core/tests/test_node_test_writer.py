"""Modified: verify runtime type guidance for node test generation and repair."""

# Modified 2026-11-02: cover Unicode-safe native-node test normalization.

from pathlib import Path
from types import SimpleNamespace

import pytest
from ag_ui_workflow import StepRunOutput

from flowx_core.llm_client.coder import MAX_TOKENS
from flowx_core.worker.node_test_writer import (
    PromptNodeTestFileCoder,
    normalize_native_node_instantiation,
)


class _FakeCompletions:
    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="def test_placeholder():\n    assert True\n"))]
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def test_node_test_writer_builds_prompt_from_node_code_file_only(tmp_path: Path) -> None:
    node_path = tmp_path / "SummarizeNode.py"
    node_path.write_text(
        "from ag_ui_workflow.nodes import WorkflowStepNode\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n\n"
        "class SummarizeNode(WorkflowStepNode):\n"
        "    STEP_ID = 'summarize_node'\n"
        "    TITLE = 'Summarize Node'\n"
        "    PROMPT = ''\n"
        "    DEPENDENCIES = ['CollectNode']\n\n"
        "    INPUT_REQUIRED = False\n\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        text = dependency_results['CollectNode'].derived.get('text', '')\n"
        "        session_state['lastSummary'] = text.upper()\n"
        "        return StepRunOutput(\n"
        "            card={'summary': text.upper()},\n"
        "            derived={'summaryText': text.upper()},\n"
        "        )\n",
        encoding="utf-8",
    )

    writer = PromptNodeTestFileCoder(client=_FakeClient())

    prompt = writer._build_user_prompt(str(node_path))

    assert "Base the tests only on this node code file" in prompt
    assert "Do not read workflow.json, requirement markdown, node_docs, or other generated node files." in prompt
    assert "Node class name: SummarizeNode" in prompt
    assert "Detected workflow base class: WorkflowStepNode" in prompt
    assert "detected session_state keys: [\"lastSummary\"]" in prompt
    assert "detected derived output keys: [\"summaryText\"]" in prompt
    assert "'CollectNode'" in prompt
    assert "WorkflowStepNode" in prompt


def test_node_test_writer_process_input_writes_test(tmp_path: Path, monkeypatch) -> None:
    writer = PromptNodeTestFileCoder(client=_FakeClient())
    node_path = tmp_path / "Example.py"
    node_path.write_text("class Example: pass\n", encoding="utf-8")
    test_path = tmp_path / "test_Example.py"
    calls = []

    def write_test(node_file_path, output_path, **options):
        calls.append((node_file_path, output_path, options))
        return Path(output_path)

    monkeypatch.setattr(writer, "write_test_from_node_file", write_test)
    result = writer.process_input("", {"upstream": StepRunOutput(derived={"ignored": True})}, {
        "PromptNodeTestFileCoder::action": "write",
        "node_test_writer": {
            "node_file_path": str(node_path),
            "output_path": str(test_path),
            "temperature": 0.4,
        },
    })

    assert result.derived == {"test_file_path": str(test_path)}
    assert calls == [(str(node_path), str(test_path), {
        "overwrite": True, "temperature": 0.4, "max_tokens": MAX_TOKENS
    })]


def test_node_test_writer_process_input_amends_existing_test(tmp_path: Path, monkeypatch) -> None:
    writer = PromptNodeTestFileCoder(client=_FakeClient())
    node_path = tmp_path / "Example.py"
    node_path.write_text("class Example(WorkflowStepNode):\n    STEP_ID = 'Example'\n", encoding="utf-8")
    test_path = tmp_path / "test_Example.py"
    test_path.write_text("def test_old():\n    assert False\n", encoding="utf-8")
    prompts = []
    monkeypatch.setattr(writer, "code_to_file", lambda prompt, path, **options: prompts.append((prompt, path, options)) or Path(path))

    result = writer.process_input("", {}, {
        "PromptNodeTestFileCoder::action": "amend",
        "node_test_writer": {
            "node_file_path": str(node_path), "output_path": str(test_path),
            "amendment": "Fix the assertion", "overwrite": True,
        },
    })

    assert result.derived == {"test_file_path": str(test_path)}
    assert "Fix the assertion" in prompts[0][0]
    assert "def test_old():" in prompts[0][0]
    assert "class Example(WorkflowStepNode):" in prompts[0][0]
    assert prompts[0][1] == str(test_path)


@pytest.mark.parametrize("state, error", [
    ({}, "node_test_writer"),
    ({"node_test_writer": {"action": "write"}}, "PromptNodeTestFileCoder::action"),
    ({"PromptNodeTestFileCoder::action": "generate", "node_test_writer": {}}, "PromptNodeTestFileCoder::action"),
    ({"PromptNodeTestFileCoder::action": "write", "node_test_writer": {}}, "node_file_path"),
    ({"PromptNodeTestFileCoder::action": "write", "node_test_writer": {"node_file_path": "node.py"}}, "output_path"),
    ({"PromptNodeTestFileCoder::action": "amend", "node_test_writer": {"node_file_path": "node.py", "output_path": "test_node.py"}}, "amendment"),
])
def test_node_test_writer_process_input_rejects_invalid_request(state, error) -> None:
    with pytest.raises(ValueError, match=error):
        PromptNodeTestFileCoder(client=_FakeClient()).process_input("", {}, state)


def test_node_test_writer_prompt_forbids_object_new_for_native_nodes(tmp_path: Path) -> None:
    node_path = tmp_path / "SummarizeNode.py"
    node_path.write_text(
        "from ag_ui_workflow.nodes import WorkflowStepNode\n\n"
        "class SummarizeNode(WorkflowStepNode):\n"
        "    STEP_ID = 'summarize_node'\n"
        "    TITLE = 'Summarize Node'\n"
        "    DEPENDENCIES = []\n"
        "    INPUT_REQUIRED = False\n",
        encoding="utf-8",
    )

    prompt = PromptNodeTestFileCoder(client=_FakeClient())._build_user_prompt(str(node_path))

    assert "never call `object.__new__(NodeClass)`" in prompt
    assert "pydaograph.GNode" in prompt


def test_node_test_generation_system_prompt_describes_runtime_variable_types() -> None:
    prompt = PromptNodeTestFileCoder(client=_FakeClient()).system_prompt

    assert "`StepMeta` is a `TypedDict` and a `dict` at runtime" in prompt
    assert '`meta["id"]`' in prompt
    assert '`meta["dependencies"]`' in prompt
    assert "`meta.id`" in prompt
    assert "`STEP_ID`, `TITLE`, and `PROMPT` are `str`" in prompt
    assert "`DEPENDENCIES` is `list[str]`" in prompt
    assert "`INPUT_REQUIRED` is `bool`" in prompt
    assert "`user_input: str`" in prompt
    assert "`file_bytes: bytes`" in prompt
    assert "`dependency_results: dict[str, StepRunOutput]`" in prompt
    assert "`StepRunOutput` is an object" in prompt
    assert "`.card` and `.derived` are `dict[str, Any]`" in prompt
    assert "`session_state: dict[str, Any]`" in prompt
    assert "`param.inputs` is `dict[str, StepRunInput]`" in prompt
    assert "`param.outputs` is `dict[str, StepRunOutput]`" in prompt
    assert "`param.state` is `dict[str, Any]`" in prompt
    assert "not necessarily `str`" in prompt
    assert "`StepRunOutput(card=..., derived=...)`" in prompt


@pytest.mark.parametrize("action", ["write", "amend"])
def test_node_test_writer_sends_dict_metadata_contract_for_write_and_amend(
    tmp_path: Path, monkeypatch, action: str
) -> None:
    node_path = tmp_path / "Example.py"
    node_path.write_text(
        "from ag_ui_workflow.nodes import WorkflowStepNode\n"
        "class Example(WorkflowStepNode):\n"
        "    STEP_ID = 'Example'\n"
        "    DEPENDENCIES = []\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "test_Example.py"
    test_path.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    writer = PromptNodeTestFileCoder(client=_FakeClient())
    prompts = []

    def capture_code(prompt, path, **options):
        prompts.append(prompt)
        return Path(path)

    monkeypatch.setattr(writer, "code_to_file", capture_code)
    if action == "write":
        writer.write_test_from_node_file(str(node_path), str(test_path))
    else:
        writer.amend_test_with_feedback(str(node_path), str(test_path), "Fix the test")

    assert len(prompts) == 1
    assert '`meta["id"] == node.STEP_ID`' in prompts[0]
    assert '`meta["dependencies"] == list(node.DEPENDENCIES)`' in prompts[0]
    assert "`meta.id`" in prompts[0]


def test_normalize_native_node_instantiation_rewrites_object_new(tmp_path: Path) -> None:
    test_path = tmp_path / "test_Example.py"
    test_path.write_text(
        "def test_a():\n"
        "    node = object.__new__(cls)\n"
        "    other = object.__new__(node_module.Example)\n"
        "    assert node is not None and other is not None\n",
        encoding="utf-8",
    )

    assert normalize_native_node_instantiation(test_path) == 2

    updated = test_path.read_text(encoding="utf-8")
    assert "node = cls()" in updated
    assert "other = node_module.Example()" in updated
    assert "object.__new__" not in updated
    assert normalize_native_node_instantiation(test_path) == 0


def test_normalize_native_node_instantiation_keeps_complex_arguments(tmp_path: Path) -> None:
    source = (
        "def test_a():\n"
        "    node = object.__new__(make_node())\n"
        "    other = object.__new__(node_module.NODES[0])\n"
        "    starred = object.__new__(*classes)\n"
        "    keyword = object.__new__(cls, flag=True)\n"
        "    multiple = object.__new__(cls, other)\n"
        "    unrelated = allocator.__new__(cls)\n"
        "    text = 'object.__new__(cls)'  # object.__new__(cls)\n"
    )
    test_path = tmp_path / "test_Example.py"
    test_path.write_text(source, encoding="utf-8")

    assert normalize_native_node_instantiation(test_path) == 0
    assert test_path.read_text(encoding="utf-8") == source


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\v", "\f"])
def test_normalize_preserves_unicode_separators_and_byte_columns(
    tmp_path: Path, separator: str
) -> None:
    source = (
        f"label = '汉{separator}字'; first = object.__new__(module.节点)\n"
        "prefix = '😊'; second = object.__new__(Example); third = object.__new__(module.节点)\n"
        f"formatted = f'🧪{separator}{{object.__new__(Example)!r}}'\n"
        "untouched = 'object.__new__(Example)'\n"
    )
    expected = (
        f"label = '汉{separator}字'; first = module.节点()\n"
        "prefix = '😊'; second = Example(); third = module.节点()\n"
        f"formatted = f'🧪{separator}{{Example()!r}}'\n"
        "untouched = 'object.__new__(Example)'\n"
    )
    test_path = tmp_path / "test_unicode.py"
    test_path.write_bytes(source.encode("utf-8"))

    assert normalize_native_node_instantiation(test_path) == 4
    assert test_path.read_bytes() == expected.encode("utf-8")
    compile(expected, str(test_path), "exec")
    assert normalize_native_node_instantiation(test_path) == 0
    assert test_path.read_bytes() == expected.encode("utf-8")


def test_normalize_multiline_dotted_argument(tmp_path: Path) -> None:
    source = "node = object.__new__(\n    module\n    .Example\n)\n"
    expected = "node = (\n    module\n    .Example\n)()\n"
    test_path = tmp_path / "test_multiline.py"
    test_path.write_bytes(source.encode("utf-8"))

    assert normalize_native_node_instantiation(test_path) == 1
    assert test_path.read_bytes() == expected.encode("utf-8")
    compile(expected, str(test_path), "exec")
    assert normalize_native_node_instantiation(test_path) == 0


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_normalize_preserves_comments_and_physical_newlines(
    tmp_path: Path, newline: str
) -> None:
    source = newline.join([
        "# header 🙂",
        "node = object.__new__(",
        "    # before argument",
        "    module  # between parts",
        "    .Example,  # after argument",
        ")  # outside call",
        "complex_node = object.__new__(module.items[0])",
    ])
    expected = newline.join([
        "# header 🙂",
        "node = (",
        "    # before argument",
        "    module  # between parts",
        "    .Example  # after argument",
        ")()  # outside call",
        "complex_node = object.__new__(module.items[0])",
    ])
    test_path = tmp_path / "test_comments.py"
    test_path.write_bytes(source.encode("utf-8"))

    assert normalize_native_node_instantiation(test_path) == 1
    assert test_path.read_bytes() == expected.encode("utf-8")
    compile(expected, str(test_path), "exec")

    class Example:
        pass

    namespace = {"module": SimpleNamespace(Example=Example, items=[Example])}
    exec(compile(expected, str(test_path), "exec"), namespace)
    assert isinstance(namespace["node"], Example)
    assert isinstance(namespace["complex_node"], Example)
    assert normalize_native_node_instantiation(test_path) == 0
    assert test_path.read_bytes() == expected.encode("utf-8")


def test_normalize_preserves_comments_in_parenthesized_callee(tmp_path: Path) -> None:
    source = (
        "node = ((object  # allocator comment\n"
        "    .__new__))(  # opening comment\n"
        "    module  # attribute comment\n"
        "    .Example,\n"
        ")\n"
    )
    test_path = tmp_path / "test_callee_comments.py"
    test_path.write_bytes(source.encode("utf-8"))

    assert normalize_native_node_instantiation(test_path) == 1
    updated = test_path.read_text(encoding="utf-8")
    for comment in ("# allocator comment", "# opening comment", "# attribute comment"):
        assert comment in updated
    compile(updated, str(test_path), "exec")

    class Example:
        pass

    namespace = {"module": SimpleNamespace(Example=Example)}
    exec(compile(updated, str(test_path), "exec"), namespace)
    assert isinstance(namespace["node"], Example)
    assert normalize_native_node_instantiation(test_path) == 0


def test_normalize_preserves_valid_backslash_continuation(tmp_path: Path) -> None:
    source = "node = object.__new__(module \\\n    .Example)\n"
    expected = "node = module \\\n    .Example()\n"
    test_path = tmp_path / "test_continuation.py"
    test_path.write_bytes(source.encode("utf-8"))

    assert normalize_native_node_instantiation(test_path) == 1
    assert test_path.read_bytes() == expected.encode("utf-8")
    compile(expected, str(test_path), "exec")
    assert normalize_native_node_instantiation(test_path) == 0


def test_normalize_compiles_before_writing(tmp_path: Path) -> None:
    source = b"return 1\nnode = object.__new__(Example)\n"
    test_path = tmp_path / "test_invalid.py"
    test_path.write_bytes(source)

    with pytest.raises(SyntaxError, match="outside function"):
        normalize_native_node_instantiation(test_path)
    assert test_path.read_bytes() == source


def test_write_test_from_node_file_normalizes_generated_instantiation(
    tmp_path: Path, monkeypatch
) -> None:
    node_path = tmp_path / "Example.py"
    node_path.write_text(
        "class Example(WorkflowStepNode):\n"
        "    STEP_ID = 'Example'\n"
        "    TITLE = 'Example'\n"
        "    DEPENDENCIES = []\n"
        "    INPUT_REQUIRED = False\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_Example.py"
    test_path.parent.mkdir()
    writer = PromptNodeTestFileCoder(client=_FakeClient())

    def fake_code_to_file(prompt, path, **options):
        Path(path).write_text(
            "def test_node():\n"
            "    node = object.__new__(Example)\n"
            "    assert node is not None\n",
            encoding="utf-8",
        )
        return Path(path)

    monkeypatch.setattr(writer, "code_to_file", fake_code_to_file)

    written = writer.write_test_from_node_file(str(node_path), str(test_path))

    assert written == test_path
    updated = test_path.read_text(encoding="utf-8")
    assert "node = Example()" in updated
    assert "object.__new__" not in updated


@pytest.mark.parametrize("action", ["write", "amend"])
def test_node_test_writer_normalizes_unicode_and_multiline_output(
    tmp_path: Path, monkeypatch, action: str
) -> None:
    source = (
        "def test_generated():\r\n"
        "    label = '前\u2028后'; node = object.__new__(Example)\r\n"
        "    other = object.__new__(\r\n"
        "        module  # keep comment\r\n"
        "        .Example,  # keep trailing comment\r\n"
        "    )\r\n"
        "    assert node is not None and other is not None\r\n"
    )
    expected = (
        "def test_generated():\r\n"
        "    label = '前\u2028后'; node = Example()\r\n"
        "    other = (\r\n"
        "        module  # keep comment\r\n"
        "        .Example  # keep trailing comment\r\n"
        "    )()\r\n"
        "    assert node is not None and other is not None\r\n"
    )
    test_path = tmp_path / "test_Example.py"
    test_path.write_text("def test_old():\n    assert True\n", encoding="utf-8")
    writer = PromptNodeTestFileCoder(client=_FakeClient())
    prompts = []
    monkeypatch.setattr(writer, "_build_user_prompt", lambda path: "node prompt")

    def fake_code_to_file(prompt, path, **options):
        prompts.append((prompt, path, options))
        Path(path).write_bytes(source.encode("utf-8"))
        return Path(path)

    monkeypatch.setattr(writer, "code_to_file", fake_code_to_file)
    output_path = str(test_path.with_suffix(""))
    if action == "write":
        written = writer.write_test_from_node_file("Example.py", output_path)
    else:
        written = writer.amend_test_with_feedback("Example.py", output_path, "Fix the test")
        assert "def test_old():" in prompts[0][0]
        assert "Fix the test" in prompts[0][0]

    assert written == test_path
    assert prompts[0][1] == str(test_path)
    assert test_path.read_bytes() == expected.encode("utf-8")
    compile(expected, str(test_path), "exec")
    assert normalize_native_node_instantiation(test_path) == 0
    assert test_path.read_bytes() == expected.encode("utf-8")


def test_node_test_writer_amend_requires_existing_test(tmp_path: Path) -> None:
    writer = PromptNodeTestFileCoder(client=_FakeClient())
    with pytest.raises(FileNotFoundError, match="Test file not found"):
        writer.process_input("", {}, {
            "PromptNodeTestFileCoder::action": "amend",
            "node_test_writer": {
                "node_file_path": str(tmp_path / "node.py"),
                "output_path": str(tmp_path / "test_node.py"),
                "amendment": "Update cases",
            },
        })