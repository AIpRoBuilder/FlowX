from typing import Any

from ag_ui_workflow import StepRunOutput, WorkflowStepNode

from flowx_core.tools.workflow_engine import (
    build_workflow_config,
    init_workflow_engine,
    run_workflow_step,
)


class _FirstStep(WorkflowStepNode):
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        del user_input, dependency_results, session_state
        return StepRunOutput(derived={"value": 2})


class _SecondStep(WorkflowStepNode):
    INPUT_REQUIRED = False

    def __init__(self) -> None:
        super().__init__()
        self.received: dict[str, StepRunOutput] = {}

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        del user_input, session_state
        self.received = dependency_results
        return StepRunOutput(derived={"value": dependency_results["first"].derived["value"] + 3})


def test_build_init_and_run_workflow_step() -> None:
    second = _SecondStep()
    steps = {"first": _FirstStep(), "second": second}
    dependencies = {"first": [], "second": ["first"]}

    config = build_workflow_config(steps, dependencies)
    engine = init_workflow_engine(
        config,
        thread_id="test-workflow-engine",
    )
    first_status = run_workflow_step(engine, "first")
    second_status = run_workflow_step(engine, "second")

    assert config.pipeline["nodes"][1]["depends"] == ["first"]
    assert first_status.isOK()
    assert second_status.isOK()
    assert engine.session.step_outputs["first"].derived == {"value": 2}
    assert second.received["first"].derived == {"value": 2}
    assert engine.session.step_outputs["second"].derived == {"value": 5}
    assert engine.reset_session().step_outputs == {}