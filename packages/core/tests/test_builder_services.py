from ag_ui_workflow import WorkflowStepNode

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_generation import NodeGenerationWorkflow


def test_node_generation_reuses_existing_workflow_steps() -> None:
    for step in (NodePlanElement, PromptNodeFileCoderBase, NodeAuditor):
        assert issubclass(step, WorkflowStepNode)
    assert NodeGenerationWorkflow.__annotations__["node_coder"] is not None