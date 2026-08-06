"""FlowX MCP server — wrap meta_agent + ag_ui_workflow as MCP tools."""

from .server import main, create_server

__all__ = ["main", "create_server"]
__version__ = "0.1.0"
