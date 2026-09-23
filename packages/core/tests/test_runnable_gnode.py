from flowx_core.tools.runnable_gnode import RunnableGNode, run_gnode_operation


class _CountingNode(RunnableGNode):
    def __init__(self) -> None:
        super().__init__()
        self.run_calls = 0

    def run(self):
        self.run_calls += 1
        return super().run()

    def add(self, left: int, right: int) -> int:
        return left + right


def test_run_gnode_operation_dispatches_through_run() -> None:
    node = _CountingNode()

    assert run_gnode_operation(node, "add", 2, 3) == 5
    assert node.run_calls == 1


def test_run_gnode_operation_supports_plain_test_doubles() -> None:
    class PlainDouble:
        def add(self, left: int, right: int) -> int:
            return left + right

    assert run_gnode_operation(PlainDouble(), "add", 2, 3) == 5