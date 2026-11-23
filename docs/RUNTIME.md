# Workflow runtime compatibility

Modified 2026-11-22: describe the workflow builder's gate steps; the LLM generation workflows are gone.

FlowX targets the request-scoped `ag_ui_workflow` API, verified against upstream
revision `0319046` (2026-10-23). That revision still reports package version `0.1.0`;
the version string alone does not distinguish the older API.

- Engine step metadata follows upstream `ag_ui_workflow.StepMeta`: `id` and
  `dependencies` are required, while `title`, `prompt`, `inputRequired`,
  `metaNodeKind`, `repeatable`, `branches`, and descriptor fields are optional.
  `init_workflow_engine` forwards each class's `step_meta()` unchanged, and
  generated backends annotate `STEP_CHAIN: list[StepMeta]`. Minimal metadata such
  as `{"id": ..., "dependencies": []}` is accepted by the engine.
- JSON pipeline pools require `node_classes`, keyed by each JSON node's `name`,
  with true concrete classes taken from a `NODES` tuple:
  `NODE_CLASSES = {node.STEP_ID: node for node in NODES}`.
  Only `name` and `depends` are needed for internal workflows; a JSON `type`, if
  included, is the concrete class name. FlowX uses no factories, partials, generated
  wrapper subclasses, or global class configuration mutation.
- The workflow builder's steps (`flowx_core.builder.gates`) are class-only: one gate class
  is created per business node with fixed `STEP_ID`/`DEPENDENCIES`, and each pooled slot
  constructs its own instance. They share one per-revision context through
  `WorkflowParam.state` (seeded by `start_steps(state=...)`), so no instance or class holds
  request state. A gate's wait function reports `awaiting_input`/`running` and the builder
  resumes it by step id with `resume_steps`.
- `init_workflow_engine(node_classes, workflow_config)` constructs the shared
  pool and engine directly, passing `node_classes` unchanged to the pool. Nodes
  declare fixed step IDs/dependencies and provide their own `step_meta()` result
  as `StepMeta`.
  No constructor inference or factory metadata is needed. JSON supplies the topology
  and optional `pool_size`; there is no separate configuration object or constructor map.
  Use `engine.run()` and `engine.answer()` for interactive runs; `engine.run_steps()`
  seeds request state and requires completion for noninteractive workflows.
  `engine.start_steps()`/`resume_steps()` are the FlowX seams with the same seeding that
  keep a paused run alive instead of closing it, so the builder can resume a gate by
  step id.
- `FlowXWorkflowEngine` is a `WorkflowEngine` that owns its pool. The workflow builder
  runs a planning engine (`requirement_gate -> graph_gate`) and a nodes engine (one gate
  per business node, `nodes_exit`, `compile_entrypoint`) and releases each when its run
  ends. `run()` accepts the native `run(request_id, inputs)` form, `answer()`, `events()`
  and `close(request_id)` remain available, and `run_steps()` is the engine-owned
  noninteractive run: it seeds `WorkflowParam.state`, applies skipped steps, and
  requires every step to complete with output. `close()` without a request id
  releases the pool and is terminal. `build_workflow_pool()` is the shared
  construction seam that validates the class map and JSON topology, and
  `init_workflow_engine()` returns a `FlowXWorkflowEngine`.
- `WorkflowFileNode` subclasses implement `process_input(file_bytes, dependency_results,
  session_state)`. Generated backends decode one file path/base64 upload to bytes;
  subclasses own processing and any persistence. Multiple uploads must be handled
  explicitly rather than relying on inherited storage.
- Nodes install their own wait callbacks. Paused requests retain a pipeline slot;
  session reset cancels the old request before rotating its ID. Pool size comes
  from the planning config and resources close at backend shutdown.
