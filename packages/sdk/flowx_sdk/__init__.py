"""Programmatic interface for building and running FlowX workflows."""

from flowx_core.agent_workflow_engine import AgentResult

from .client import FlowXClient, result_to_dict

__all__ = ["AgentResult", "FlowXClient", "result_to_dict"]
