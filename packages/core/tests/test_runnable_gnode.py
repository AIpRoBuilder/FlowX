from ag_ui_workflow import StepRunOutput, WorkflowStepNode
import pytest

from flowx_core.tools.runnable_gnode import RunnableGNode, run_gnode_operation


class _CountingNode(RunnableGNode):
    def __init__(self) -> None:
        super().__init__()
        self.process_input_calls = 0

    def process_input(self, user_input, dependency_results, session_state):
        self.process_input_calls += 1
        return super().process_input(user_input, dependency_results, session_state)

    def add(self, left: int, right: int) -> int:
        return left + right


def test_run_gnode_operation_dispatches_through_process_input() -> None:
    node = _CountingNode()

    assert run_gnode_operation(node, "add", 2, 3) == 5
    assert node.process_input_calls == 1


def test_runnable_gnode_implements_process_input() -> None:
    node = _CountingNode().prepare_run("add", 2, 3)

    output = node.process_input("", {}, {})

    assert RunnableGNode.process_input is not WorkflowStepNode.process_input
    assert output == StepRunOutput(derived={"result": 5})
    assert node.run_result == 5


def test_run_gnode_operation_supports_plain_test_doubles() -> None:
    class PlainDouble:
        def add(self, left: int, right: int) -> int:
            return left + right

    assert run_gnode_operation(PlainDouble(), "add", 2, 3) == 5


def test_run_gnode_operation_preserves_step_output() -> None:
    node = _CountingNode()
    expected = StepRunOutput(derived={"value": 5})

    node.output = lambda: expected

    assert run_gnode_operation(node, "output") is expected


def test_run_gnode_operation_normalizes_component_errors() -> None:
    node = _CountingNode()

    def fail() -> None:
        raise ValueError("invalid")

    node.fail = fail

    with pytest.raises(RuntimeError, match="ValueError: invalid"):
        run_gnode_operation(node, "fail")