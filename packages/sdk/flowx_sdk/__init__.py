"""Programmatic interface for building and running FlowX workflows."""

from flowx_core.agent_intent import WORKFLOW_TARGET
from flowx_core.agent_workflow_engine import AgentResult
from flowx_core.dynamic_workflow_orchestrator import NodeRepairInput

from .client import FlowXClient, result_to_dict

__all__ = [
    "AgentResult", "FlowXClient", "NodeRepairInput", "WORKFLOW_TARGET", "result_to_dict",
]
