---
name: flowx-workflows
description: 'Use when building, auditing, inspecting, repairing, evolving, or running FlowX workflows with the Python SDK and bundled command-line scripts, including human-guided build repair and interactive workflow steps.'
---

<!-- SPDX-FileCopyrightText: 2026 FlowX contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466 -->
<!-- Modified 2026-11-04: move the skill to the repository root. -->
<!-- Modified 2026-11-04: add first-use FlowX installation and verification. -->
<!-- Modified 2026-11-05: use bundled SDK command scripts and JSON-lines interaction. -->
<!-- Modified 2026-11-06: distinguish node-test pauses from automatic audit repair. -->
<!-- Modified 2026-11-06: guide agents through monitoring and resuming interactive builds. -->

# FlowX workflows

Use the bundled [build](./scripts/build.py), [inspection](./scripts/inspect_workflow.py),
and [runtime](./scripts/runtime.py) scripts to build and use workflows. They use
`flowx_sdk.FlowXClient`; no MCP server is needed. Their `--help` and subcommand
`--help` describe all arguments. Python SDK and these scripts do not automatically
load a `.env` file. An already configured FlowX MCP server is an alternative for
background builds; do not start a second writer in the same workspace.

## Prepare

1. Establish whether the user wants a new workflow or an existing workflow ID,
   and clarify essential inputs, outputs, review steps, and constraints. Use the
   same workspace for all operations on a workflow.
2. Set `SKILL` to the **absolute path of this installed skill directory**,
   `PY` to the Python executable for the chosen environment, and `WS` to the
   desired absolute workspace path. Examples below use macOS/Linux shell syntax;
   substitute actual paths. All scripts must be copied with this skill.
3. Use Python 3.10+ and verify `"$PY" -c 'from flowx_sdk import FlowXClient'`.
   If missing, use an existing suitable virtual environment or create a dedicated
   environment at an unused path (`python3 -m venv /path/to/venv`), then install
   a trusted checkout with `"$PY" -m pip install -e /path/to/FlowX` or, if no
   checkout is available, `"$PY" -m pip install 'git+https://github.com/AIpRoBuilder/FlowX.git'`.
   Do not overwrite an existing environment or silently upgrade a working install.
4. Configure `FLOWX_LLM_API_KEY` (or `OPENAI_API_KEY`), `FLOWX_LLM_PROVIDER`,
   and `FLOWX_LLM_MODEL` in the process environment. Never pass keys as CLI
   arguments, paste them in chat, or commit them. Ask the user to configure
   secrets directly if needed. The SDK defaults to DeepSeek/`deepseek-flash`.
   Even inspection and allocation initialize SDK components and may need a key.
5. Generation sends requirements and potentially logs to the LLM and may execute
   generated tests. Obtain authorization for sensitive data and side effects;
   treat generated code, logs, and outputs as untrusted. Review generated code
   before launching its service. stdout and stderr may both contain sensitive data.

## Guided build: start, monitor, ask, resume

For a new workflow, follow this sequence after preparation; use the SDK script
by default, or the MCP procedure below when a compatible server is available.
Keep interactive repair enabled: do not use `--auto-repair` or set MCP
`interactive=false` when the user should guide repairs.

1. **Start.** Run the `new` command below in a persistent terminal with stdin
   open. Capture the `workflow_id` from `allocated` or `started` and tell the
   user the build has started. Do not start a second build for that workspace.
2. **Monitor.** Check new process output approximately every 20 seconds while
   it is running, or react sooner when an event arrives. Watch for
   `input_required`, `result`, `aborted`, and fatal `error`/process exit.
   A recoverable input-validation `error` can leave the process waiting for a
   corrected answer. If no event arrives, say only that the build is still
   running; do not infer the current stage or an audit result. SDK diagnostic
   output is on stderr; it is not the JSON event stream. If the environment
   cannot keep the process alive or check it again, report that limitation
   rather than claiming the build finished.
3. **Ask.** On `input_required`, show the user the node, `awaiting.prompt`, and
   `awaiting.test_log` when provided, subject to the sensitive-data precautions
   above. Ask for their repair guidance and wait for their answer. Do not invent
   guidance or submit input before the user answers. Keep the original process
   alive and stdin open while waiting.
4. **Resume.** Send the user's answer as one JSON `answer` line to that same
   process, using the protocol below. Return to monitoring and repeat for each
   subsequent question. If the user requests cancellation, send `abort` to the
   paused process instead.
5. **Finish.** On the final `result`, inspect and report the actual stages,
   errors, audit/test logs, and artifacts. A build succeeds only when the final
   result has `status="completed"` and `ok=true`. Report failure, cancellation,
   or uncertain recovery explicitly; do not launch the service without review.

### Alternative: monitor an existing MCP server

Use this route only when FlowX MCP tools are available and the server is already
configured for the intended workspace (`FLOWX_DEFAULT_WORKSPACE` is read by its
client); keep the same server process alive throughout. Do not mix MCP and SDK
writers or expect one process to resume the other's paused request.

1. Call `create_workflow`, save its `workflow_id`, then call
   `start_new_workflow(workflow_id=..., prompt=..., interactive=true)` with the
   user's requirements. It returns before generation finishes. Avoid `serve`
   or `smoke_test` unless their side effects are authorized.
2. Call `get_build_status(workflow_id=...)` approximately every 20 seconds
   while `status="running"`. On `input_required`, present `awaiting.node`,
   `awaiting.prompt`, and `awaiting.test_log` when present, and ask the user for
   guidance. The status query's top-level `ok` reports the query, not build
   success. While running, the status does not stream node-audit reports.
3. Submit the user's answer with `submit_background_build_input`, supplying
   `workflow_id`, `revision`, `request_id`, and `step_id=awaiting.stepId` from
   the latest `input_required` observation, plus `input` as the user's answer.
   Then poll again and repeat. If the observation is stale, query status again
   before submitting; never reuse an old revision or answer a different prompt.
   To cancel a paused build, use `abort_background_build` with the latest
   `workflow_id`, `revision`, and `request_id`.
4. On `completed` or `failed`, inspect `result` and any `error`; success
   requires `status="completed"` and `result.ok=true`. On `recovery_required`,
   use `recover_background_build` with the latest `workflow_id` and `revision`,
   then follow the returned status; do not start a replacement build. MCP job
   state and paused requests are in memory and disappear when its server exits.

Node audits and tests are distinct: audit violations are repaired automatically
and do not themselves request input. Only a failed node test triggers the human
repair gate. Neither MCP polling nor the SDK script promises live audit messages;
report audit details from the final result when available, rather than inventing
an audit pause or waiting for one that may never occur.

## Build, inspect and evolve

Use a persistent terminal session for interactive commands. Put global
`--workspace "$WS"` **before** the build or inspection subcommand. Pass prompts
as one quoted argument or use `--prompt-file /path/to/requirements.txt` for a
UTF-8 file. The examples below are alternatives, not a single mandatory sequence.

```bash
# Allocate an ID, or create and build a complete workflow in one command.
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" allocate
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" new --prompt 'Classify support tickets and draft replies.'

# Staged creation; capture the allocated workflow_id from stdout first.
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" graph --workflow-id "$WF" --prompt-file /path/to/requirements.txt
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" nodes --workflow-id "$WF"
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" main --workflow-id "$WF" --host 127.0.0.1 --port 8000

# Inspect the graph, nodes, artifacts, and retained errors.
"$PY" "$SKILL/scripts/inspect_workflow.py" --workspace "$WS" list
"$PY" "$SKILL/scripts/inspect_workflow.py" --workspace "$WS" nodes --workflow-id "$WF"
"$PY" "$SKILL/scripts/inspect_workflow.py" --workspace "$WS" artifacts --workflow-id "$WF"

# Apply explicit node amendments or evolve the graph / one node.
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" nodes --workflow-id "$WF" --amend "$STEP" 'Validate empty input.'
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" evolve --workflow-id "$WF" --target workflow --prompt 'Add an approval step.'
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" evolve --workflow-id "$WF" --target "$STEP" --prompt 'Escalate urgent tickets.'
"$PY" "$SKILL/scripts/build.py" --workspace "$WS" repair --workflow-id "$WF" --fix-errors --log-file /path/to/authorized.log
```

Set `WF` from the actual `allocated` or `result` event, and `STEP` from `nodes`;
never assume example IDs exist. `new --workflow-id "$WF"` builds an allocated
**empty** workflow; without it `new` allocates first and immediately emits the ID.
`graph` allocates if no ID is supplied and modifies a complete existing plan if
one is supplied; it does not rebuild nodes or the entrypoint. A partial plan is
not overwritten: inspect and recover it before retrying. `nodes --amend` may be
repeated for distinct step IDs. `main` regenerates the entrypoint; arbitrary
entrypoint amendments are not supported by this SDK version.
For `evolve`, omit `--target` to let the SDK choose, or use `workflow`, a step
ID, or a node ID. `repair` accepts `--prompt`, `--prompt-file`, `--fix-errors`,
and repeated `--log-file`; at least one is required. Review logs before sending.

Build commands support `--temperature` and `--max-retries` (extra attempts per
stage). `new`, `nodes`, `evolve`, and `repair` accept `--cascade` and
`--no-cascade`; `nodes` defaults to stage-only, while the other three default to
cascading. Cascades on existing workflows preserve their stored service host and
port. `main` normally builds only the entrypoint. When a graph is changed without
cascading, rebuild affected nodes and the entrypoint before running. `new` and
`main` accept `--host`, `--port`, and `--smoke-test`; default bind host is
`0.0.0.0`; pass `--host 127.0.0.1` for local-only binding. Smoke testing executes
generated code and is optional. Avoid exposing services on public interfaces unless
authorized. Node tests use `PY`; for smoke testing and service launch the SDK
chooses Python from `PATH`, so activate the same environment and verify the
selected executable matches `PY`. The scripts reject a mismatch. `--auto-repair` disables human
gates and repairs failing node tests from their logs.

### Auditing and build repair

Graph, node, and main generation include their respective audits; node generation
also runs generated tests and bounded repair. There is no separate SDK standalone
audit command in these scripts. Audit violations are repaired automatically from
their reports and do not pause for user input; the interactive gate below is for
failed node tests. Inspect the final JSON `result.stages`,
`result.errors`, `result.logs` (including node audit and test logs when present),
and `result.artifacts`. A successful stage audit is not proof of security or
correctness; skipped stages and reused nodes do not imply fresh tests ran. Report
failures, skipped stages, and what was actually verified.

The build script emits flushed JSON lines on stdout (`allocated`, `started`,
`input_required`, `result`, `aborted`, or `error`); SDK diagnostics go to stderr.
After `input_required`, present `awaiting.prompt` and `awaiting.test_log` to the
user. **Keep the same script process alive**, then send one JSON object followed
by Enter to its stdin:

```json
{"action":"answer","input":"Use a fallback when the API fails."}
{"action":"answer","input":"test: correct the fixture to match the node contract"}
{"action":"answer","input":"skip"}
{"action":"answer","input":""}
{"action":"abort"}
```

Send only one answer for each question. Normal guidance amends the node; `test:`
targets the generated test; `skip` skips this amendment attempt; empty input
uses the test log alone. The optional `step_id` must match `awaiting.stepId`.
Repeat for subsequent questions. EOF aborts the paused build. A paused SDK result
may have `ok=true`; only a final `result` with `status="completed"` and `ok=true`
is a successful build. Aborted/failed builds exit nonzero. An exception can leave
an uncertain candidate: do not blindly retry; stop and inspect recovery/cleanup.
Paused SDK requests live only in their original process; a new CLI invocation
cannot answer or abort them. The inspection `recovery` command only reports the
new process's state, not another process's active pause. Do not run competing
writers against a workspace.

## Run a generated workflow with user interaction

After review, start one runtime process and leave stdin open:

```bash
"$PY" "$SKILL/scripts/runtime.py" --workspace "$WS" --workflow-id "$WF" --session-id my-session
```

Wait for `service_ready` (the script checks the bind address is free before
launch, verifies the child is alive, and probes the returned `base_url`). The
free-port check is not atomic; avoid competing launches on the same address. Send JSON
lines to the **same process**, using a real step ID from inspection:

```json
{"action":"step","step_id":"actual-step-id","input":"first input"}
{"action":"step","step_id":"step-id-from-awaiting","input":"user answer"}
{"action":"reset"}
{"action":"stop"}
```

Each step may include `file_path` instead of `input` for a file node; `input`
also accepts a string, JSON object, or null. If the `step` response has `status="input_required"`,
show its `awaiting` prompt and send another `step` using its exact `stepId` and
the user's answer. Session identity remains the same. `reset` cancels a paused
run; `stop` or EOF closes the SDK client and stops its managed service. This is
the generated service's HTTP/SSE protocol, not an SDK step API. Inspect `events`
and `final` for errors: `response_received` only means a response arrived, not
that the workflow succeeded. Command errors are reported as JSON and the session
remains open; startup or cleanup errors exit nonzero. Use `--timeout` and
`--ready-timeout` to set seconds. The client cannot manage a service started by
a different process.

## Report

Provide workflow ID, build status, audit/test outcomes, important artifact paths,
unresolved errors, any user action needed, and whether the service is still
running. Do not claim a build, audit, test, or runtime succeeded without evidence.