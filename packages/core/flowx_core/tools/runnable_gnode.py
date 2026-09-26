from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from ag_ui_workflow import WorkflowOperationNode
from pydaograph import CStatus


RunnableGNodeType = TypeVar("RunnableGNodeType", bound="RunnableGNode")


class RunnableGNode(WorkflowOperationNode):
    """A workflow operation node that executes one configured method through run()."""

    def __init__(self) -> None:
        super().__init__()
        self._run_operation: Callable[[], Any] | None = None
        self._run_result: Any = None

    def prepare_run(
        self: RunnableGNodeType,
        operation_name: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> RunnableGNodeType:
        operation = getattr(self, operation_name, None)
        if not callable(operation):
            raise AttributeError(f"{type(self).__name__} has no callable '{operation_name}' operation.")

        self._run_operation = lambda: operation(*args, **kwargs)
        self._run_result = None
        return self

    @property
    def run_result(self) -> Any:
        return self._run_result

    def run(self) -> CStatus:
        if self._run_operation is None:
            return CStatus(1001, "No operation has been configured. Call prepare_run() before run().")

        try:
            self._run_result = self._run_operation()
            return CStatus()
        except Exception as exc:
            return CStatus(1001, f"{type(exc).__name__}: {exc}")


def run_gnode_operation(
    component: Any,
    operation_name: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute a RunnableGNode through run(), preserving test-double compatibility."""

    if not isinstance(component, RunnableGNode):
        return getattr(component, operation_name)(*args, **kwargs)

    status = component.prepare_run(operation_name, *args, **kwargs).run()
    if status.isErr():
        raise RuntimeError(status.getInfo())
    return component.run_result