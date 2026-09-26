from pathlib import Path
from types import SimpleNamespace

from ag_ui_workflow import WorkflowOperationNode

from flowx_core.runtime import RunNodeTest
from flowx_core.worker.node_test_writer import PromptNodeTestFileCoder
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_runtime_modifier import (
    AmendNodeFromTestLog,
    ExecuteNodeTest,
    GenerateNodeTestFile,
    NodeRuntimeModifierPipeline,
)


class _TestWriter(GenerateNodeTestFile):
    def __init__(self) -> None:
        WorkflowOperationNode.__init__(self)
        self.calls: list[tuple[str, str]] = []

    def write_test_from_node_file(self, node_file_path: str, output_path: str) -> Path:
        self.calls.append((node_file_path, output_path))
        target = Path(output_path)
        target.write_text("def test_generated():\n    assert True\n", encoding="utf-8")
        return target


class _NodeWriter(AmendNodeFromTestLog):
    def __init__(self) -> None:
        WorkflowOperationNode.__init__(self)
        self.calls: list[dict[str, object]] = []

    def amend_code_with_feedback(
        self,
        code_path: str,
        amendment: str,
        **kwargs: object,
    ) -> Path:
        self.calls.append(
            {
                "code_path": code_path,
                "amendment": amendment,
                **kwargs,
            }
        )
        target = Path(code_path)
        target.write_text("# amended node\n", encoding="utf-8")
        return target


def test_node_runtime_modifier_stages_inherit_their_operational_components() -> None:
    assert issubclass(GenerateNodeTestFile, PromptNodeTestFileCoder)
    assert issubclass(ExecuteNodeTest, RunNodeTest)
    assert issubclass(AmendNodeFromTestLog, PromptNodeFileCoderBase)


def test_node_runtime_modifier_generates_runs_and_amends_from_test_log(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    test_writer = _TestWriter()
    node_writer = _NodeWriter()
    observed_command: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed_command.extend(command)
        assert kwargs["cwd"] == str(tmp_path)
        return SimpleNamespace(returncode=1, stdout="1 failed\n", stderr="AssertionError\n")

    modifier = NodeRuntimeModifierPipeline(
        node_test_writer=test_writer,
        node_writer=node_writer,
        run_subprocess=fake_run,
        timeout_expired=TimeoutError,
    )

    context = modifier.run(
        node_file_name="ExampleNode.py",
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
        python_command="/custom/python",
        timeout=15,
    )

    assert modifier.json_config()["nodes"][1]["depends"] == ["generate_node_test"]
    assert test_writer.calls == [
        (str(node_file), str(tmp_path / "tests" / "test_ExampleNode.py"))
    ]
    assert observed_command == [
        "/custom/python",
        "-m",
        "pytest",
        str(tmp_path / "tests" / "test_ExampleNode.py"),
        "-q",
    ]
    assert context.test_result is not None
    assert context.test_result["ok"] is False
    assert "Return code: 1" in context.test_log
    assert node_writer.calls == [
        {
            "code_path": str(node_file),
            "amendment": context.test_log,
            "graph_plan_path": str(tmp_path / "workflow.json"),
            "current_node_name": "ExampleNode",
        }
    ]
    assert Path(context.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# amended node\n"


def test_node_runtime_modifier_can_skip_every_stage(tmp_path: Path) -> None:
    node_file = tmp_path / "ExampleNode.py"
    node_file.write_text("# original node\n", encoding="utf-8")
    test_writer = _TestWriter()
    node_writer = _NodeWriter()

    def unexpected_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise AssertionError("the test subprocess must not run")

    modifier = NodeRuntimeModifierPipeline(
        node_test_writer=test_writer,
        node_writer=node_writer,
        run_subprocess=unexpected_run,
        timeout_expired=TimeoutError,
    )

    context = modifier.run(
        node_file_name="ExampleNode.py",
        workflow_graph={"nodes": [{"name": "ExampleNode", "depends": []}]},
        working_directory=str(tmp_path),
        skip_generate_node_test=True,
        skip_run_node_test=True,
        skip_amend_node=True,
    )

    assert context.skipped_stages == ["generate_node_test", "run_node_test", "amend_node"]
    assert test_writer.calls == []
    assert node_writer.calls == []
    assert Path(context.amended_node_file_path) == node_file
    assert node_file.read_text(encoding="utf-8") == "# original node\n"