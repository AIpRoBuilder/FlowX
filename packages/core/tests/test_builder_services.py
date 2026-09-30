from ag_ui_workflow import WorkflowStepNode

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.worker.node_writer import PromptNodeFileCoderBase
from flowx_core.workflows.node_generation import NodePlanningNode, NodeWritingNode, NodeAuditingNode


def test_node_generation_stages_are_specialized_workflow_steps() -> None:
    for stage, base in (
        (NodePlanningNode, NodePlanElement),
        (NodeWritingNode, PromptNodeFileCoderBase),
        (NodeAuditingNode, NodeAuditor),
    ):
        assert issubclass(stage, base)
        assert issubclass(stage, WorkflowStepNode)
        assert stage.process_input is not WorkflowStepNode.process_input