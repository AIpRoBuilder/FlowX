"""Private construction-scoped settings for concrete pooled node instances."""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


_NODE_SETTINGS: ContextVar[Mapping[str, Mapping[str, Any]] | None] = ContextVar(
    "flowx_node_settings", default=None,
)


@contextmanager
def node_settings(settings: Mapping[str, Mapping[str, Any]]) -> Generator[None, None, None]:
    """Scope shallow settings to synchronous pool construction, then restore them."""
    token = _NODE_SETTINGS.set({step_id: dict(values) for step_id, values in settings.items()})
    try:
        yield
    finally:
        _NODE_SETTINGS.reset(token)


def apply_node_settings(node: Any) -> None:
    """Apply settings once per instance, before subclass prompt composition."""
    if isinstance(node, type):
        raise TypeError("node settings require an instance, not a class")
    if vars(node).get("_flowx_node_settings_applied", False):
        return
    settings = _NODE_SETTINGS.get()
    if settings is not None:
        for name, value in settings.get(node.STEP_ID, {}).items():
            setattr(node, name, value)
    node._flowx_node_settings_applied = True