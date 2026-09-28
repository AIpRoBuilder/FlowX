from types import SimpleNamespace

from ag_ui_workflow import WorkflowStepNode

from flowx_core.builder_services import _NodeGenerationNode


def test_node_generation_node_implements_process_input() -> None:
    progress_messages: list[str] = []
    worker = SimpleNamespace(
        node_name="ExampleNode",
        builder=SimpleNamespace(_advance_progress=progress_messages.append),
        generate=lambda: "/tmp/ExampleNode.py",
    )
    node = _NodeGenerationNode(worker)

    output = node.process_input("", {}, {})

    assert _NodeGenerationNode.process_input is not WorkflowStepNode.process_input
    assert output.card == {"kind": "node-generation", "status": "completed"}
    assert output.derived == {"generated_path": "/tmp/ExampleNode.py"}
    assert progress_messages == ["Generated node: ExampleNode"]