"""Build and run reusable AG-UI workflows from JSON-named concrete nodes.

Modified: pass explicit concrete class maps directly; use class-declared identities.
Modified: seed the attached WorkflowParam.state through a request-local lease bridge.
Modified 2026-10-16: reload an engine's pool and step metadata when the workflow config changes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ag_ui_workflow import (
    RunResult,
    StepRunInput,
    StepRunOutput,
    WorkflowEngine,
    WorkflowParam,
    WorkflowPipelinePool,
)
from pydaograph import GElement, GPipeline


_RUN_REQUEST: ContextVar[tuple[Mapping[str, Any], frozenset[str]] | None] = ContextVar(
    "flowx_workflow_request", default=None,
)


class _WorkflowPipelinePool(WorkflowPipelinePool):
    """Seed the leased parameter before native execution, not inside node wrappers."""

    @contextmanager
    def lease(self) -> Generator[tuple[GPipeline, WorkflowParam], None, None]:
        with super().lease() as (pipeline, param):
            request = _RUN_REQUEST.get()
            if request is not None:
                state, skipped_steps = request
                param.state.update(state)
                for step_id in skipped_steps:
                    output = StepRunOutput(card={"status": "skipped"})
                    param.complete(step_id, output, output.card)
            yield pipeline, param


def _prepare_graph(
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]], int]:
    """Check a class map against its JSON topology; return native JSON, step metadata and pool size.

    Class STEP_ID and DEPENDENCIES must match the JSON topology. Metadata comes
    directly from each class without constructing speculative node instances.
    """
    nodes = workflow_config.get("nodes")
    if not isinstance(nodes, list) or not nodes or any(not isinstance(node, dict) for node in nodes):
        raise ValueError("workflow configuration requires a non-empty nodes list")
    names = [node.get("name") for node in nodes]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("workflow node names must be non-empty strings")
    if len(names) != len(set(names)) or set(names) != set(node_classes):
        raise ValueError("workflow configuration must define each node class exactly once")
    pool_size = workflow_config.get("pool_size", 1)
    if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1:
        raise ValueError("workflow pool_size must be a positive integer")

    native_nodes, steps_meta = [], []
    for node in nodes:
        name = node["name"]
        node_class = node_classes[name]
        if not isinstance(node_class, type) or not issubclass(node_class, GElement):
            raise TypeError(f"node '{name}' must map to a concrete GElement class")
        dependencies = node.get("depends", [])
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
            raise ValueError(f"node '{name}' depends must be a list of node names")
        if name in dependencies:
            raise ValueError(f"node '{name}' cannot depend on itself")
        if node.get("loop", 1) != 1:
            raise ValueError("internal workflows must be single-pass (loop=1)")
        if node_class.STEP_ID != name or list(node_class.DEPENDENCIES) != dependencies:
            raise ValueError(f"node '{name}' class STEP_ID/DEPENDENCIES must match the workflow configuration")
        native_nodes.append({"name": name, "depends": dependencies})
        steps_meta.append(node_class.step_meta())
    return json.dumps({"nodes": native_nodes}), steps_meta, pool_size


def init_workflow_engine(
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
    *,
    graph_config_id: str = "flowx",
) -> WorkflowEngine:
    """Pass the supplied concrete class map directly to a reusable pipeline pool."""
    pipeline_json, steps_meta, pool_size = _prepare_graph(node_classes, workflow_config)
    pool = _WorkflowPipelinePool(
        pipeline_json=pipeline_json,
        node_classes=node_classes,
        size=pool_size,
    )
    try:
        return WorkflowEngine(
            graph_config_id=graph_config_id,
            steps_meta=steps_meta,
            pool=pool,
        )
    except BaseException:
        pool.close()
        raise


def reload_workflow_engine(
    engine: WorkflowEngine,
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
) -> None:
    """Serve new runs from a changed graph on the same engine and pool.

    The pool builds the new graph first, so an invalid config leaves the current
    graph and engine metadata untouched. Leases already out finish on the old graph;
    ``run_workflow`` snapshots step ids when a run starts.
    """
    if not isinstance(engine.pool, _WorkflowPipelinePool):
        raise TypeError("only an engine created by init_workflow_engine can be reloaded")
    pipeline_json, steps_meta, pool_size = _prepare_graph(node_classes, workflow_config)
    engine.pool.reload(pipeline_json=pipeline_json, node_classes=node_classes, size=pool_size)
    # The engine has no reload API and keeps its own step metadata and lookup map.
    engine.steps_meta = engine._normalize_steps_meta(steps_meta)
    engine._step_map = {step["id"]: step for step in engine.steps_meta}


def run_workflow(
    engine: WorkflowEngine,
    *,
    request_id: str | None = None,
    state: Mapping[str, Any] | None = None,
    inputs: Mapping[str, StepRunInput] | None = None,
    skipped_steps: Iterable[str] = (),
) -> RunResult:
    """Seed native WorkflowParam.state and run once with the original inputs.

    ``state`` is only a seed mapping, not a second session object. The native
    pool owns the attached dictionary passed by identity to every step and
    clears it when the lease ends. Nested request values retain their identity.
    """

    step_ids = [step["id"] for step in engine.steps_meta]
    skipped = frozenset(skipped_steps)
    unknown = (set(inputs or {}) | skipped).difference(step_ids)
    if unknown:
        raise ValueError(f"unknown workflow step(s): {sorted(unknown)}")
    if not isinstance(engine.pool, _WorkflowPipelinePool) and (state or skipped):
        raise TypeError("request state/skipping requires a pool created by init_workflow_engine")
    token = _RUN_REQUEST.set((state if state is not None else {}, skipped))
    try:
        result = engine.run(request_id or f"flowx-request-{uuid.uuid4().hex}", dict(inputs or {}))
    finally:
        _RUN_REQUEST.reset(token)
    if result.status != "completed":
        engine.close(result.request_id)
        step_id = result.failed_step or (result.awaiting or {}).get("id", "")
        raise RuntimeError(f"workflow step '{step_id}' failed: {result.error or result.status}")
    missing = set(step_ids).difference(result.outputs)
    if missing:
        raise RuntimeError(f"workflow execution did not produce output for step(s): {sorted(missing)}")
    return result


def close_workflow_engine(engine: WorkflowEngine) -> None:
    """Cancel outstanding requests before destroying their pool."""

    try:
        engine.close()
    finally:
        engine.pool.close()


__all__ = [
    "close_workflow_engine",
    "init_workflow_engine",
    "reload_workflow_engine",
    "run_workflow",
]
