from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from ag_ui_workflow import StepRunOutput, WorkflowStepNode


RunnableGNodeType = TypeVar("RunnableGNodeType", bound="RunnableGNode")


class RunnableGNode(WorkflowStepNode):
    """A workflow step node that executes one configured method through run()."""

    INPUT_REQUIRED = False

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

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input, dependency_results, session_state
        if self._run_operation is None:
            raise RuntimeError("No operation has been configured. Call prepare_run() before process_input().")

        self._run_result = self._run_operation()
        if isinstance(self._run_result, StepRunOutput):
            return self._run_result
        return StepRunOutput(derived={"result": self._run_result})

def run_gnode_operation(
    component: Any,
    operation_name: str,
    /,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Execute a RunnableGNode through its step hook, preserving test-double compatibility."""

    if not isinstance(component, RunnableGNode):
        return getattr(component, operation_name)(*args, **kwargs)

    try:
        component.prepare_run(operation_name, *args, **kwargs).process_input("", {}, {})
    except Exception as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
    return component.run_result