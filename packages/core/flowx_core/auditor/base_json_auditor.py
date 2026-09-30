"""Base workflow node for auditors that operate on graph data instead of files."""

from ag_ui_workflow import WorkflowStepNode


class BaseJsonAuditor(WorkflowStepNode):
    """Common base for graph JSON audits."""
