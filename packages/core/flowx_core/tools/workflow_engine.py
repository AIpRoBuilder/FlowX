"""Build and run reusable AG-UI workflows from JSON-named concrete nodes.

Modified: pass explicit concrete class maps directly; use class-declared identities.
Modified: seed the attached WorkflowParam.state through a request-local lease bridge.
Modified 2026-10-16: reload an engine's pool and step metadata when the workflow config changes.
Modified 2026-10-23: type engine step metadata with ag_ui_workflow.StepMeta.
Modified 2026-10-24: add FlowXWorkflowEngine so generation workflows are engines, not wrappers;
run noninteractive graphs through the engine's own ``run_steps``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from ag_ui_workflow import (
    GraphSpec,
    RunResult,
    StepMeta,
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
) -> tuple[str, list[StepMeta], int]:
    """Check a class map against its JSON topology; return native JSON, step metadata and pool size.

    Class STEP_ID and DEPENDENCIES must match the JSON topology. Metadata comes
    directly from each class without constructing speculative node instances and
    follows the upstream ``StepMeta`` contract (``id``/``dependencies`` required).
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

    native_nodes: list[dict[str, Any]] = []
    steps_meta: list[StepMeta] = []
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


def build_workflow_pool(
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
) -> tuple[WorkflowPipelinePool, list[StepMeta]]:
    """Validate a concrete class map against its JSON topology and build its pool."""
    pipeline_json, steps_meta, pool_size = _prepare_graph(node_classes, workflow_config)
    pool = _WorkflowPipelinePool(
        pipeline_json=pipeline_json,
        node_classes=node_classes,
        size=pool_size,
    )
    return pool, steps_meta


def init_workflow_engine(
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
    *,
    graph_config_id: str = "flowx",
) -> FlowXWorkflowEngine:
    """Build the reusable pipeline pool and its FlowX engine in one step."""
    return FlowXWorkflowEngine(
        node_classes,
        workflow_config,
        graph_config_id=graph_config_id,
    )


class FlowXWorkflowEngine(WorkflowEngine):
    """A WorkflowEngine that owns its FlowX pool and runs request models directly.

    Concrete workflows declare the request types they accept in ``REQUEST_TYPES``
    and implement ``execute``. ``run`` accepts either such a request or the native
    ``request_id``/``inputs`` form, so inherited ``answer`` and ``events`` keep the
    upstream engine contract. ``close(request_id)`` abandons one waiting run;
    ``close()`` releases the pool and is terminal.
    """

    REQUEST_TYPES: ClassVar[tuple[type[Any], ...]] = ()
    CLOSED_MESSAGE: ClassVar[str] = "workflow engine is closed"

    def __init__(
        self,
        node_classes: Mapping[str, type[GElement]],
        workflow_config: Mapping[str, Any],
        *,
        graph_config_id: str = "flowx",
    ) -> None:
        pool, steps_meta = build_workflow_pool(node_classes, workflow_config)
        try:
            super().__init__(
                graph_config_id=graph_config_id,
                steps_meta=steps_meta,
                pool=pool,
            )
        except BaseException:
            pool.close()
            raise
        self._closed = False

    @classmethod
    def from_graph_spec(
        cls,
        graph: GraphSpec,
        *,
        graph_config_id: str,
        pool_size: int = 1,
    ) -> "FlowXWorkflowEngine":
        """Build an engine from an already merged graph, including loop wrappers.

        ``build_workflow_pool`` derives step metadata from top-level node classes,
        which cannot represent a ``GRegion``/``WorkflowLoopRegion`` factory.  This
        constructor uses the spec's materialized JSON, class map and step metadata
        directly while retaining the FlowX state-seeding pool and ``run_steps``.
        """
        engine = cls.__new__(cls)
        engine._init_from_graph_spec(
            graph,
            graph_config_id=graph_config_id,
            pool_size=pool_size,
        )
        return engine

    def _init_from_graph_spec(
        self,
        graph: GraphSpec,
        *,
        graph_config_id: str,
        pool_size: int = 1,
    ) -> None:
        """Initialize this instance from an already merged graph spec."""
        if isinstance(pool_size, bool) or not isinstance(pool_size, int) or pool_size < 1:
            raise ValueError("pool_size must be a positive integer")
        pool = _WorkflowPipelinePool(
            pipeline_json=graph.pipeline_json,
            node_classes=graph.node_classes,
            size=pool_size,
        )
        try:
            WorkflowEngine.__init__(
                self,
                graph_config_id=graph_config_id,
                steps_meta=graph.steps_meta,
                pool=pool,
                graph=graph,
            )
            self._closed = False
        except BaseException:
            pool.close()
            raise

    def reload_graph_spec(
        self,
        graph: GraphSpec,
        *,
        pool_size: int | None = None,
    ) -> None:
        """Serve new runs from a merged graph while retaining this engine.

        The pool builds the replacement graph before swapping it in, so a failed
        build leaves the current graph and metadata untouched.  Active leases
        finish on their old pipelines.  Callers must not change step metadata
        while a run is still consuming the old graph.
        """
        if self._closed:
            raise RuntimeError(self.CLOSED_MESSAGE)
        if not isinstance(self.pool, _WorkflowPipelinePool):
            raise TypeError("only a FlowX workflow engine can reload a graph spec")
        self.pool.reload(
            pipeline_json=graph.pipeline_json,
            node_classes=graph.node_classes,
            size=pool_size,
        )
        self.graph = graph
        self.steps_meta = self._normalize_steps_meta(graph.steps_meta)
        self._step_map = {step["id"]: step for step in self.steps_meta}
        self._loop_bindings = dict(graph.loop_bindings)
        self.loop_key = graph.loop_key

    @property
    def closed(self) -> bool:
        """Whether ``close()`` has released this engine's pool."""
        return self._closed

    def execute(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one request model; each concrete workflow implements this."""
        raise NotImplementedError(f"{type(self).__name__} does not implement execute()")

    def run(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a request model, or delegate the native request-id form upstream."""
        if self._closed:
            raise RuntimeError(self.CLOSED_MESSAGE)
        if isinstance(request, self.REQUEST_TYPES):
            return self.execute(request, *args, **kwargs)
        return super().run(request, *args, **kwargs)

    def run_steps(
        self,
        *,
        request_id: str | None = None,
        state: Mapping[str, Any] | None = None,
        inputs: Mapping[str, StepRunInput] | None = None,
        skipped_steps: Iterable[str] = (),
    ) -> RunResult:
        """Seed native WorkflowParam.state and run the whole graph once.

        ``state`` is only a seed mapping, not a second session object. The native
        pool owns the attached dictionary passed by identity to every step and
        clears it when the lease ends. Nested request values retain their identity.
        The run must complete and produce every step's output.
        """

        step_ids = [step["id"] for step in self.steps_meta]
        skipped = frozenset(skipped_steps)
        unknown = (set(inputs or {}) | skipped).difference(step_ids)
        if unknown:
            raise ValueError(f"unknown workflow step(s): {sorted(unknown)}")
        token = _RUN_REQUEST.set((state if state is not None else {}, skipped))
        try:
            # Bypass this class's request-model dispatch: a native run starts
            # from a request id and must not be re-dispatched as a request.
            result = super().run(
                request_id or f"flowx-request-{uuid.uuid4().hex}", dict(inputs or {}),
            )
        finally:
            _RUN_REQUEST.reset(token)
        if result.status != "completed":
            self.close(result.request_id)
            step_id = result.failed_step or (result.awaiting or {}).get("id", "")
            raise RuntimeError(f"workflow step '{step_id}' failed: {result.error or result.status}")
        missing = set(step_ids).difference(result.outputs)
        if missing:
            raise RuntimeError(f"workflow execution did not produce output for step(s): {sorted(missing)}")
        return result

    def close(self, request_id: str | None = None) -> None:
        """Abandon one waiting run, or release the pool when called without an id."""
        if request_id is not None:
            super().close(request_id)
            return
        if self._closed:
            return
        self._closed = True
        close_workflow_engine(self)


def reload_workflow_engine(
    engine: WorkflowEngine,
    node_classes: Mapping[str, type[GElement]],
    workflow_config: Mapping[str, Any],
) -> None:
    """Serve new runs from a changed graph on the same engine and pool.

    The pool builds the new graph first, so an invalid config leaves the current
    graph and engine metadata untouched. Leases already out finish on the old graph;
    ``run_steps`` snapshots step ids when a run starts.
    """
    if not isinstance(engine.pool, _WorkflowPipelinePool):
        raise TypeError("only an engine created by init_workflow_engine can be reloaded")
    pipeline_json, steps_meta, pool_size = _prepare_graph(node_classes, workflow_config)
    engine.pool.reload(pipeline_json=pipeline_json, node_classes=node_classes, size=pool_size)
    # The engine has no reload API and keeps its own step metadata and lookup map.
    engine.steps_meta = engine._normalize_steps_meta(steps_meta)
    engine._step_map = {step["id"]: step for step in engine.steps_meta}


def close_workflow_engine(engine: WorkflowEngine) -> None:
    """Cancel outstanding requests before destroying their pool.

    Calls the upstream ``close`` directly so engines that release their pool in
    an overridden ``close()`` do not re-enter it from here.
    """

    try:
        WorkflowEngine.close(engine)
    finally:
        engine.pool.close()


__all__ = [
    "FlowXWorkflowEngine",
    "build_workflow_pool",
    "close_workflow_engine",
    "init_workflow_engine",
    "reload_workflow_engine",
]
