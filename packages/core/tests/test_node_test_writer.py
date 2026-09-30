from pathlib import Path
from types import SimpleNamespace

import pytest
from ag_ui_workflow import StepRunOutput

from flowx_core.llm_client.coder import MAX_TOKENS
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder


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