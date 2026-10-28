# Dynamic workflow orchestration

Modified 2026-10-15: document the initial versioned generation coordinator.
Modified 2026-10-15: schedule generation through JSON-built WorkflowEngine graphs.
Modified 2026-10-16: AgentBuilder publishes the child workflows; one engine reloads its pool.
Modified 2026-10-28: expand each business node into namespaced generation, audit-guard, repair-loop and finalizer steps in one engine.
Modified 2026-10-28: expose the expanded graph as `ExpandedWorkflowEngine` and diff it in place from a changed business DAG.

`DynamicWorkflowOrchestrator` coordinates node generation and isolated node repair.
It does **not** execute the generated business workflow, edit graph topology through
an LLM, or provide a ReAct judge. Supply the desired workflow JSON to each run.

## Ownership and construction

Import the coordinator and `DynamicWorkflowRequest` from
`flowx_core.dynamic_workflow_orchestrator`.

The keyword-only constructor accepts:

- `storage_dir`: a dedicated directory for **one** logical workflow.
- `workflow_id`: optional on first creation; generated automatically if omitted.
  Reloading the same directory restores its identity. A conflicting supplied ID
  is rejected.
- `keep_last_versions`: `None` keeps all history; a positive integer retains the
  most recent N workflow revisions and, independently, N revisions per node.

The node generation workflow and runtime modifier are **not** constructor
arguments. `AgentBuilder` creates them and publishes them as the module globals
`NODE_GENERATION_WORKFLOW` and `NODE_RUNTIME_MODIFIER_PIPELINE`.
`AgentBuilder.close()` unpublishes only its own workflows before closing them.
`run()` fails before creating any files if they are not published, so a
`DynamicWorkflowOrchestrator` needs a live `AgentBuilder` in the process. The
globals are process-wide: with several builders, the most recently initialized one
is used. The orchestrator never closes them; `close()` releases only its engine.

## Expanded business engine

When the published workflows expose native `GraphSpec`s, the coordinator expands
each enabled business node into prefixed native steps inside one engine:

```text
<node>__enter
  -> <node>__node_plan
  -> <node>__node_generate
  -> <node>__node_audit
  -> <node>__repair_audit_guard
  -> <node>__generate_node_test
  -> <node>__repair_loop
       <node>__run_node_test
       -> <node>__amend_node
       -> <node>__verify_node_test
       -> <node>__audit_amended_node
       -> <node>__amend_from_audit
  -> <node>__finalize
```

A business edge `A -> B` becomes `B__enter depends on A__finalize`. The
finalizer is therefore the completion boundary for the whole generation, audit,
repair and re-audit chain. `B__enter` receives only its direct parents'
finalizer outputs; each finalizer carries cumulative ancestor artifacts.

The repair chain is a native loop region. Its loop policy stops when the verify
test and amended audit both pass, when the request's `max_repair_retries` budget
is exhausted, or when the node is reused/repair was disabled. The guard sits
outside the loop and disables repair when generation's audit failed. If tests
pass but the audit fails, `amend_from_audit` applies the violation report before
the next verification pass; one extra pass is allowed to verify that amendment.

Each business node has a request namespace in `WorkflowParam.state` containing
its generation state and runtime-modifier request. Namespaced step classes read
only their own namespace. This lets independent business branches run
concurrently without overwriting another node's `node_writer`, `auditor` or
`node_runtime_modifier` state, and keeps prompt context local to the node.

The published node-generation workflow and runtime modifier must expose their
native `GraphSpec`s. Publishing graph-less workflow objects is rejected at
engine construction time; there is no wrapper-step fallback.

The native `WorkflowParam.state` carries one request-local generation-run payload,
the node namespaces and the node logs. Pooled steps retain no requests or results.
After the engine finishes, the caller publishes finalizer records and updates the
manifest. Child-step exceptions become failed step outputs and block descendants;
independent branches drain before the caller raises and removes the candidate.

`generation_engine` exposes the one `ExpandedWorkflowEngine`, or `None` before
the first nonempty run. `ExpandedWorkflowEngine.__init__` builds the merged
generation/repair graph, and `update(business_nodes=...)` diffs the newly
supplied business DAG against the current one. Unchanged chains keep their
existing graph classes and metadata; only added, removed, changed, and their
dependent chains are rebuilt. The underlying pipeline pool builds the replacement
before swapping it in, so active leases finish on the old graph and a failed
build leaves the current graph serving. A changed enabled topology updates the
engine in place while preserving unaffected chains; request content changes do
not rebuild it. The JSON `pool_size` defaults to 1 and controls concurrent
whole-workflow runs. An all-disabled graph publishes a workflow snapshot with an
empty node map and schedules nothing.

## Requests and incremental generation

Create `DynamicWorkflowRequest(workflow_json_path=..., requirement_md_path=...)`
and pass it to `orchestrator.run(request)`. The result is a `WorkflowVersion`.
A runnable, LLM-free expanded-engine smoke example is in
[`examples/dynamic_workflow_orchestrator.py`](../examples/dynamic_workflow_orchestrator.py).
It writes a persistent bundle under `examples/output/dynamic_workflow_orchestrator`
with generated node source/test files and runs an incremental business-graph update.

The coordinator validates node names, dependencies, disabled-node dependencies,
duplicate names and cycles before invoking either child. Nodes are processed in
dependency order by `WorkflowEngine`, not by a Python generation loop.
Source, static output previews and session-state key names
from already generated ancestors become the next node's `dependency_context`.
These are **static generation inputs**, not actual runtime outputs. Each step
publishes its own artifact facts in its `StepRunOutput` (`derived["artifacts"]`,
cumulative over its ancestors), and a child builds its context and ancestor
digests from its parents' outputs plus the run-level `session_state`, so no
source file is re-read or re-parsed per descendant.

New nodes are generated. Existing nodes are amended when their specification,
global requirement, generation options, explicit `node_formats`, or ancestor
source changes. An unchanged build returns the current version without calling
either child or incrementing the version counter. Graph-only metadata changes
can produce a new workflow version while reusing every node version.

Request options include:

- `node_amendments` and `markdown_amendments`, keyed by node name.
- `force_nodes` for explicitly rebuilding selected nodes.
- `node_formats` for extra generation contract context.
- `generate_markdown` and `skip_node_planner` for planner control.
- `run_runtime_modifier` (default `True`) for isolated pytest generation/repair.
- `max_repair_retries` (default `2`) for how many more times a failing repair is requested.
- `state` for request-local context; it is not serialized in the manifest.
- Generation/test temperatures, Python command and per-test timeout.

Because runtime `state` can contain non-serializable values, it is not part of
the persistent fingerprint. Use `force_nodes` or an amendment when state-only
changes should trigger regeneration. Requirements are currently global: changing
them conservatively invalidates all enabled nodes. Python is the supported node
language for this initial coordinator.

A repair pass tests the node, and when its test fails it amends the source from the
log, reruns the existing test without another amendment, and re-audits the source.
If a pass still fails its test or audit, another pass is requested, up to
`max_repair_retries` more times (`0` restores a single pass). The generated test is
reused; a failing test is amended from its fresh log, and an audit violation is
amended from the violation report before the node is retested and re-audited. A
still-failing node is not published. A failed build does not advance
the workflow/node counters or replace historical files. Disabling the modifier
does not imply runtime validation; old test logs are not carried into changed,
untested node versions. When `repair_input_callback` is set, it is called after a
failed test with a `NodeRepairInput` (node, attempt, test log and result). Its
return value guides the amendment, `skip` skips that amendment, and the next
retry asks again with the fresh log.

## Saved identities and versions

Public accessors:

- `workflow_id`, `workflow_version`.
- `generated_node_id_map`: current enabled node name to persistent node UUID.
- `list_workflow_versions()` and `get_workflow_version(version=None)`.
- `list_node_versions(node_id)` and `get_node_version(node_id, version=None)`.
- `prune_versions()` to apply the configured retention policy immediately.

Each `WorkflowVersion` includes a saved JSON path, saved requirement path,
`changed_nodes`, and a node-name-to-`NodeVersion` mapping. Each `NodeVersion`
includes `node_id`, `version`, `input_fingerprint` and artifact paths for source,
optional markdown, test, log and pre-repair failure log.

The storage layout is:

```text
manifest.json
workflows/<workflow-version>/
  workflow.json
  generation_workflow.json
  requirement_analysis.md
  version.json
  <NodeName>.py
  node_docs/<NodeName>.md
  tests/test_<NodeName>.py
  logs/...
nodes/<node-id>/<node-version>/
  version.json
  <NodeName>.py
  node_docs/<NodeName>.md
  tests/test_<NodeName>.py
  logs/...
```

Versions start at 1 and remain monotonic after pruning. A workflow revision pins
each node's identity and version. It contains independent artifact copies, so
pruning a node's standalone history does not break retained workflow bundles.
Literal generated test paths are rebased to their saved bundle; logs retain the
original paths as historical execution evidence.

Deleting a node removes it from the current ID map, not from historical records.
Re-adding the same name restores its identity. Renaming is currently remove+add,
not an identity-preserving rename. Returned dataclasses describe snapshots;
callers should not edit the saved files or mutate artifact mappings.

## Persistence boundaries

Files are written to a candidate directory and the manifest is atomically
replaced only after all selected nodes succeed. Retention is applied at reload
and after successful publication. Failed ordinary requests clean their candidate
files. Pruned versions cannot be retrieved through the history APIs.

Use one writer per storage directory. Calls on the same instance are serialized;
this is not a multi-process/distributed revision database. An interrupted process
can leave an unreferenced candidate directory: a subsequent build refuses to
overwrite it. Inspect and remove that orphan only after confirming no writer is
still active; historical snapshots referenced by the manifest must not be removed.

Generated Python tests execute code through the existing modifier subprocess;
that subprocess is **not a security sandbox**. Use trusted generators/nodes or
provide an externally isolated execution environment. Runtime session persistence,
side-effect rollback and full workflow observation collection are separate work.