from ag_ui_workflow import WorkflowOperationNode

from flowx_core.architect.graph_planner import GraphPlanner
from flowx_core.architect.node_planner import NodePlanner
from flowx_core.auditor.context_auditor import ContextAuditor
from flowx_core.auditor.graph_json_auditor import GraphJsonAuditor
from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.auditor.output_auditor import OutputAuditor
from flowx_core.demand_analyzer.requirement_disector import RequirementDisector
from flowx_core.worker.main_writer import PromptMainFileCoder


def test_planners_writers_and_auditors_subclass_workflow_operation_node() -> None:
    instances = [
        GraphPlanner(client=object()),
        NodePlanner(client=object()),
        RequirementDisector(client=object()),
        PromptMainFileCoder(),
        ContextAuditor(),
        GraphJsonAuditor(),
        MainEntryPointAuditor(),
        NodeAuditor(),
        OutputAuditor(),
    ]

    for instance in instances:
        assert isinstance(instance, WorkflowOperationNode)