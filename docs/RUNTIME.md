# Workflow runtime compatibility

Modified 2026-11-22: describe the workflow builder's gate steps; the LLM generation workflows are gone.
Modified 2026-11-23: document FlowX-owned workflow file integrity checks, evolve modes, and optional OS sandboxing.
Modified 2026-11-25: update WorkflowFileNode contract to files: dict[str, bytes] and multi-file HTTP decoding.

FlowX targets the request-scoped `ag_ui_workflow` API, verified against upstream
revision `27a8b4f` (with the `dict[str, bytes]` file-node update, 2026-11-25). That
revision still reports package version `0.1.0`; the version string alone does not
distinguish the older API.

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
- `WorkflowFileNode` subclasses implement `process_input(files, dependency_results,
  session_state)` where `files` is a non-empty `dict[str, bytes]` mapping file names
  to binary payloads. Generated backends decode one or multiple file-path/base64
  uploads into `dict[str, bytes]`; subclasses own processing and any persistence.
- Nodes install their own wait callbacks. Paused requests retain a pipeline slot;
  session reset cancels the old request before rotating its ID. Pool size comes
  from the planning config and resources close at backend shutdown.

## Workflow file ownership, integrity, and evolve modes

- FlowX owns `<workspace>/workflows/<workflow_id>/` (`plan/`, `nodes/`,
  `workflows/`, `manifest.json`, `agent.json`, `build/`, and rendered files in
  `service/service_<id>/`). Only `service/service_<id>/.env` is user-editable
  runtime configuration.
- FlowX records SHA-256 digests when writing `plan/*`, publishing node versions,
  pinning workflow snapshots, and rendering the service package. `verify_workflow`
  checks those digests; `begin_build`, `begin_evolve`, `submit_task`,
  `read_artifact`, and `run_service` fail closed (`WorkflowFilesModified` /
  `workflow_files_modified_outside_flowx`) when FlowX-owned files are modified
  outside FlowX. `run_service` also re-renders `service/service_<id>/`
  deterministically from the verified snapshot before spawning (preserving `.env`).
- Revisions carry an explicit mode (`Mode.BUILD`, `Mode.EDIT_NODE`,
  `Mode.EDIT_GRAPH`):
  - `begin_evolve(..., scope="node", node=...)` runs in `edit_node` mode (body
    engine only, graph topology frozen, verified at `NodesExit`). With default
    `cascade="revalidate"`, compatible downstream nodes are re-tested and
    re-published automatically without issuing an agent task; broken children
    open a task with findings and the upstream `source_diff`.
  - `begin_evolve(..., scope="graph", update_requirement=...)` runs in
    `edit_graph` mode (head engine then body engine). Plan writes are staged in
    memory and committed only after `NodesExit` publishes the new workflow
    snapshot, so aborting or failing mid-edit leaves `plan/` matching the
    previous snapshot.
- In-process digest checks and MCP/skill rules provide detection and capability
  boundaries, not DRM or OS-level tamper-proofing. When running untrusted agents
  that have shell access on the same host, mount `<workspace>/workflows` read-only
  in the agent sandbox (for example `bwrap --ro-bind` on Linux or a `:ro` volume
  mount in a container) and grant write access only to the FlowX/MCP process.
