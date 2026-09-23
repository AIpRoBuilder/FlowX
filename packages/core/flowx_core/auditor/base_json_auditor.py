
from flowx_core.auditor.base_auditor import BaseAuditor


class BaseJsonAuditor(BaseAuditor):
    """Shared parent for JSON-based auditors."""

    def __init__(self) -> None:
        super().__init__()
