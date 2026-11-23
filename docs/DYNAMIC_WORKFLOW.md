# Workflow builder and service manager

Modified 2026-10-15: document the initial versioned generation coordinator.
Modified 2026-10-15: schedule generation through JSON-built WorkflowEngine graphs.
Modified 2026-10-16: AgentBuilder publishes the child workflows; one engine reloads its pool.
Modified 2026-10-28: expand each business node into namespaced generation, audit-guard, repair-loop and finalizer steps in one engine.
Modified 2026-10-28: expose the expanded graph as `ExpandedWorkflowEngine` and diff it in place from a changed business DAG.
Modified 2026-10-30: the orchestrator is an `ExpandedWorkflowEngine`; a transit `nodes_stage_exit` step publishes the run.
Modified 2026-11-01: an initial generation audit failure enters the repair loop; only failing tests use the interactive gate.
Modified 2026-11-02: correct the audit guard behavior and default repair retry count.
Modified 2026-11-22: replace the orchestrator documentation. FlowX no longer calls an LLM: a host coding agent writes
the artifacts, and the workflow builder is a deterministic pipeline of contracts, audits, tests and versions.

FlowX manages two things.

- **`WorkflowBuilder`** (`flowx_core.builder`) turns a request into published artifacts. It sends the host agent
  *contract tasks*, checks what comes back with the auditors and pytest, versions what passes, and renders the service
  entrypoint. It never writes code and never calls a model.
- **`ServiceManager`** (`flowx_core.service`) runs, drives and inspects the services the builder produced.

`AgentBuilder` (`flowx_core.agent_builder`) owns both for one workspace; `FlowXClient`, the MCP server and the A2A
adapter are thin layers over the same two objects.

## The pipeline

A build is a native `ag_ui_workflow` run, so the pipeline keeps the engine's step structure: dependency edges, per-step
state, and waiting steps. Its steps are *agent gates*: a step whose wait function pauses the run until the agent submits
an artifact, then verifies it.

```text
planning run (only when requirement or graph is asked)
  requirement_gate -> graph_gate

nodes run (graph read from plan/workflow.json after planning)
  <Node>__author  (one gate per enabled business node, edges = the graph's depends)
      \-> nodes_exit -> compile_entrypoint
```

- `requirement_gate`: accepts `requirement` (Markdown), checked for the required sections and a node design table.
- `graph_gate`: accepts `graph` (workflow.json as an object or text), checked structurally (unique names, dependencies,
  cycles, enabled/disabled consistency) and by `GraphJsonAuditor`.
- `<Node>__author`: accepts `code` and `test` (and optionally `markdown`). The candidate is staged with the graph and the
  published upstream nodes, audited by `NodeAuditor`, and its pytest module runs offline (`FLOWX_MEDIA_DRY_RUN=1`, time
  limited). Only a candidate that passes both is published as a new immutable node version, immediately.
- `nodes_exit`: if every node is published or reused, pins the latest node versions as a workflow snapshot.
- `compile_entrypoint`: copies the snapshot into `service/service_<id>/`, renders `main.py` from the template
  (`EntrypointWriter`), audits it with `MainEntryPointAuditor`, and optionally starts it once (`smoke_test`).

Independent nodes are open at the same time. A rejected submission re-issues the same task with `attempt + 1`, the
structured findings and the failing test log; after `max_attempts` the node is `failed` and its descendants `blocked`.
Nodes that already published stay published.

## Task packets

`get_tasks` and `submit_task` exchange `ContractPacket`s:

| Field | Meaning |
| --- | --- |
| `task_id` | `<stage>:<step>:r<revision>:a<attempt>`; a stale id is rejected. |
| `stage`, `step_id` | `requirement`, `graph` or `node` with the node name. |
| `reason` | `new`, `modify`, `targeted`, `spec_changed`, `upstream_changed`, or `repair`. |
| `contract` | `instructions`, `rules` (the same ids the auditors report), `schema`, `node_types`, and for nodes `test_instructions`. |
| `context` | The requirement, graph, node spec, upstream node sources with `derived_keys` and `session_state_keys`, the current artifacts, `last_findings`, `last_test_log`, and `runtime_evidence` for a targeted rewrite. |
| `meta` | For nodes, the base-class reference for the node's `meta_node_kind`. |
| `submit` | The artifact slots: name, path inside the version, required or not. |

Contract text lives next to the code that enforces it (`flowx_core/builder/contract_text`, `findings.py`); there is no
copy in the skill. `CONTRACT_VERSION` is stored with every node version.

A finding is `{rule_id, message, file, line, severity, remediation}`. Auditor rule ids are namespaced (`node.`, `graph.`,
`main.`, `requirement.`); `node.test_failed` carries the log tail.

## State and events

`get_build_state` returns, per revision: `status` (`running`, `completed`, `failed`, `aborted`, or `idle` /
`interrupted` when no build is open), `phase`, `stages` (requirement, graph, nodes, entrypoint), `nodes` (`blocked`,
`ready`, `validating`, `repair_needed`, `published`, `reused`, `failed`), open task summaries, unresolved `errors`
(keys `graph`, `main`, `nodes/<step>`), and `next_action`:

| `next_action.kind` | Agent should |
| --- | --- |
| `submit_task` | answer the listed `task_ids` |
| `wait` | poll again; a verification is running |
| `run_service` | the build completed |
| `escalate_to_user` | the build failed; read `errors` and the findings |
| `begin_build` | no build is open |

Every change is an event appended to `<workflow>/build/events.jsonl` (`build_started`, `stage`, `phase`, `node`,
`task_issued`, `task_closed`, `build_finished`) and then reduced into that state, so the journal and the board cannot
disagree. `get_build_events(since=<seq>)` reads the journal incrementally.

## Reuse, versions and changes

A node version records `input_fingerprint`, a hash of the node's spec in `workflow.json`, the source digests of its
parents, and a scheme marker. On the next `begin_build`:

- a node whose fingerprint is unchanged is `reused`; no task is issued;
- a changed spec or a changed parent asks for the node again (`spec_changed` / `upstream_changed`), with the published
  files in `context.current`;
- `target=<node name or node_id>` forces one node (`targeted`), then its descendants follow by the rule above;
- `target="workflow"` asks for the requirement and graph again; `stages` selects them explicitly;
- nodes published by an earlier FlowX build (no scheme marker) are reused as they are, never rewritten implicitly.

A build that stops early (abort, failure, process exit) keeps every node it published, so the next `begin_build` asks
only for what is missing. Build state is in memory; a restarted process reports `interrupted` and recovers by
`begin_build`.

## Storage layout

```text
<workspace>/workflows/<workflow_id>/
  plan/{workflow.json, requirement_analysis.md, brief.md}
  nodes/<node_id>/<version>/        every published node version: code, tests/, node_docs/, logs/, version.json
  workflows/<version>/              self-contained workflow snapshots: workflow.json, requirement, node files
  manifest.json                     node registry and workflow history (schema_version 1)
  agent.json                        ids, entrypoint, host/port and unresolved errors
  service/service_<main_id>/        importable package with main.py, workflow.json and the node modules
  logs/                             service.log, main_run.log
  build/                            events.jsonl and per-revision staging (removed after a completed build)
  runs/                             run journal written by the generated service
```

## Service manager

`run_service` starts `main.py` with the interpreter from `select_python_command()` and the service folder's `.env`,
logs to `logs/service.log`, and waits until `GET /` answers. `run_step` and `reset_session` use the generated
service's `/api/run-step` and `/api/reset-session`; `list_runs`, `get_run` and `evidence` read `runs/`. `evidence`
returns the latest run journal and the service log tail for `begin_build(runtime_evidence=..., target=<node>)`.
