"""Run configured workflow step instances through ``WorkflowEngine``."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ag_ui_workflow import StepRunOutput, WorkflowEngine, WorkflowStepNode
from pydaograph import CStatus, register_class


@dataclass(frozen=True)
class WorkflowEngineConfig:
    pipeline: dict[str, Any]
    steps_meta: list[dict[str, Any]]
    step_classes: tuple[type[WorkflowStepNode], ...]


def _step_adapter_class(
    step_id: str,
    dependencies: list[str],
    target: WorkflowStepNode,
) -> type[WorkflowStepNode]:
    def process_input(
        self: WorkflowStepNode,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        return target.process_input(user_input, dependency_results, session_state)

    class_name = f"FlowXStep_{uuid.uuid4().hex}"
    adapter_class = type(
        class_name,
        (WorkflowStepNode,),
        {
            "STEP_ID": step_id,
            "TITLE": step_id,
            "DEPENDENCIES": dependencies,
            "INPUT_REQUIRED": False,
            "process_input": process_input,
        },
    )
    register_class(adapter_class)
    return adapter_class


def build_workflow_config(
    steps: Mapping[str, WorkflowStepNode],
    dependencies: Mapping[str, Iterable[str]],
) -> WorkflowEngineConfig:
    """Build engine JSON and metadata for configured workflow step instances."""

    step_classes = {
        step_id: _step_adapter_class(
            step_id,
            [dependency for dependency in dependencies.get(step_id, ()) if dependency in steps],
            step,
        )
        for step_id, step in steps.items()
    }
    pipeline = {
        "nodes": [
            {
                "name": step_id,
                "type": step_class.__name__,
                "meta_type": "WorkflowStepNode",
                "depends": list(step_class.DEPENDENCIES),
                "loop": 1,
            }
            for step_id, step_class in step_classes.items()
        ]
    }
    return WorkflowEngineConfig(
        pipeline=pipeline,
        steps_meta=[step_class.step_meta() for step_class in step_classes.values()],
        step_classes=tuple(step_classes.values()),
    )


def init_workflow_engine(
    config: WorkflowEngineConfig,
    *,
    thread_id: str | None = None,
) -> WorkflowEngine:
    """Initialize a ``WorkflowEngine`` from a built configuration."""

    temporary_dir = TemporaryDirectory(prefix="flowx-workflow-")
    config_path = Path(temporary_dir.name) / "workflow.json"
    config_path.write_text(json.dumps(config.pipeline), encoding="utf-8")
    engine = WorkflowEngine(
        pipeline_json_path=str(config_path),
        steps_meta=config.steps_meta,
        thread_id=thread_id or f"flowx-{uuid.uuid4().hex}",
    )
    setattr(engine, "_flowx_temporary_directory", temporary_dir)
    setattr(engine, "_flowx_step_classes", config.step_classes)
    return engine


def run_workflow_step(
    engine: WorkflowEngine,
    step_id: str,
    user_input: Any = None,
) -> CStatus:
    """Run one engine step and return its successful status."""

    status = engine.run_step(step_id, user_input)
    if status.isErr():
        raise RuntimeError(f"workflow step '{step_id}' failed: {status.getInfo()}")
    return status


__all__ = [
    "WorkflowEngineConfig",
    "build_workflow_config",
    "init_workflow_engine",
    "run_workflow_step",
]
