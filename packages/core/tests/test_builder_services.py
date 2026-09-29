from ag_ui_workflow import WorkflowStepNode

from flowx_core.workflows.node_generation import NodeGeneratorNode


def test_node_generator_is_the_workflow_step() -> None:
    assert issubclass(NodeGeneratorNode, WorkflowStepNode)
    assert NodeGeneratorNode.process_input is not WorkflowStepNode.process_input