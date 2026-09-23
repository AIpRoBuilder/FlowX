from pathlib import Path
from types import SimpleNamespace

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
        "from ag_ui_workflow.nodes import WorkflowOperationNode\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n\n"
        "class SummarizeNode(WorkflowOperationNode):\n"
        "    STEP_ID = 'summarize_node'\n"
        "    TITLE = 'Summarize Node'\n"
        "    PROMPT = ''\n"
        "    DEPENDENCIES = ['CollectNode']\n\n"
        "    def process_operation(self, dependency_results, session_state):\n"
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
    assert "Detected workflow base class: WorkflowOperationNode" in prompt
    assert "detected session_state keys: [\"lastSummary\"]" in prompt
    assert "detected derived output keys: [\"summaryText\"]" in prompt
    assert "'CollectNode'" in prompt
    assert "WorkflowOperationNode" in prompt