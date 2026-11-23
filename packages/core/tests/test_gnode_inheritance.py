from ag_ui_workflow import WorkflowStepNode

from flowx_core.auditor.graph_json_auditor import GraphJsonAuditor
from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.auditor.output_auditor import OutputAuditor
from flowx_core.builder.gates import CompileEntrypoint, GraphGate, NodesExit, RequirementGate, node_gate_class


def test_auditors_and_build_steps_subclass_workflow_step_node() -> None:
    instances = [
        GraphJsonAuditor(),
        MainEntryPointAuditor(),
        NodeAuditor(),
        OutputAuditor(),
        RequirementGate(),
        GraphGate(),
        node_gate_class("Example", [])(),
        NodesExit(),
        CompileEntrypoint(),
    ]

    for instance in instances:
        assert isinstance(instance, WorkflowStepNode)
