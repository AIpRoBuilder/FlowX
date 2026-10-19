# Workflow runtime compatibility

FlowX targets the request-scoped `ag_ui_workflow` API, verified against upstream
revision `3e1e15b` (2026-10-13). That revision still reports package version `0.1.0`;
the version string alone does not distinguish the older API.

- JSON pipeline pools require `node_classes`, keyed by each JSON node's `name`,
  with true concrete classes taken from a `NODES` tuple:
  `NODE_CLASSES = {node.STEP_ID: node for node in NODES}`.
  Only `name` and `depends` are needed for internal workflows; a JSON `type`, if
  included, is the concrete class name. FlowX uses no factories, partials, generated
  wrapper subclasses, or global class configuration mutation.
- Internal generation/repair workflows construct the actual node types once per
  pool slot. Callers explicitly snapshot dataclass init fields, retaining shallow
  references to injected clients and recording sinks. The private
  `node_settings(settings)` construction scope applies these values once to each
  instance before prompt composition and restores its `ContextVar` in `finally`.
  Settings are isolated between concurrent construction contexts, not stored on
  classes. Replacing a prototype or monkeypatching its methods afterward does not
  change pooled nodes.
  Use request state for per-run settings and `reset_llm_config()` to recreate
  pools after changing LLM configuration.
- `init_workflow_engine(node_classes, workflow_config)` constructs the shared
  pool and engine directly, passing `node_classes` unchanged to the pool. Nodes
  declare fixed step IDs/dependencies and provide their own `step_meta()`.
  Configured workflows scope instance settings around this initialization call;
  no constructor inference or factory metadata is needed. JSON supplies the topology
  and optional `pool_size`; there is no separate configuration object or constructor map.
  Use `engine.run()` and `engine.answer()` for interactive runs; `run_workflow()`
  requires completion for the noninteractive generation/repair workflows.
- `WorkflowFileNode` subclasses implement `process_input(file_bytes, dependency_results,
  session_state)`. Generated backends decode one file path/base64 upload to bytes;
  subclasses own processing and any persistence. Multiple uploads must be handled
  explicitly rather than relying on inherited storage.
- Nodes install their own wait callbacks. Paused requests retain a pipeline slot;
  session reset cancels the old request before rotating its ID. Pool size comes
  from the planning config and resources close at backend shutdown.
