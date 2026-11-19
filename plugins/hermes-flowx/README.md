<!-- SPDX-FileCopyrightText: 2026 FlowX contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466 -->
<!-- Upstream: https://github.com/AIpRoBuilder/FlowX -->
<!-- Added 2026-11-19: document the Hermes FlowX viewer, tools, installation, and safety. -->

# FlowX for Hermes Agent

A **read-only** FlowX integration for [Hermes Agent](https://hermes-agent.nousresearch.com/):

- a **FlowX dashboard tab** with an interactive plan graph, run journal cards,
  and a workflow file browser;
- five agent tools for listing workflows, reading plans, inspecting runs, and
  reading bounded text artifacts.

It is not a port of `plugins/deepseek-harness-flowx`. Hermes uses a Python
directory plugin (`plugin.yaml` + `register(ctx)`) plus an optional dashboard
extension (`dashboard/manifest.json` + a pre-built JS bundle + FastAPI routes),
so the DSH/Cordis host and browser bundle cannot be reused.

## What the viewer shows

- Workflow list with version, node count, build state, errors, and latest run.
- The `plan/workflow.json` graph as a draggable canvas. Positions are saved to
  `canvas_layout.json`; this is the only file the viewer writes.
- Per-node metadata, dependency edges, node artifacts (`source`, `test`, `log`,
  ...), and the latest run status.
- Run journals from `runs/<run_id>/run.json`: status, timestamps, per-step
  cards, prompts, and image/video media through bounded Range requests.
- A workflow folder browser with text and media previews.

The viewer has no build, run, repair, evolve, abort, or settings controls. Use
the FlowX skill or MCP server for those operations.

## Agent tools

| Tool | Purpose |
|---|---|
| `flowx_list_workflows` | List workflows with versions, node counts, errors, and latest run. |
| `flowx_get_workflow` | Read the plan graph, node artifacts, main entrypoint, and optional requirement analysis. |
| `flowx_list_runs` | List recent run journals. |
| `flowx_get_run` | Read one run journal; omit `run_id` for the latest. |
| `flowx_read_file` | Read a bounded UTF-8 artifact such as a plan, requirement, node source/test/log, or run record. |

## Install

### Local checkout

From the FlowX repository root:

```bash
mkdir -p ~/.hermes/plugins
cp -R plugins/hermes-flowx ~/.hermes/plugins/flowx

hermes plugins validate plugins/hermes-flowx
hermes plugins enable flowx
hermes config set plugins.entries.flowx.settings.workspace /absolute/path/to/flowx_workspaces
hermes dashboard
```

`hermes plugins enable` is an explicit opt-in and may show a capability consent
prompt. The dashboard tab appears after a dashboard restart or
`curl http://127.0.0.1:9119/api/dashboard/plugins/rescan`.

### From Git

Hermes can install a plugin subdirectory from a repository:

```bash
hermes plugins install AIpRoBuilder/FlowX/plugins/hermes-flowx --enable
```

Review the source and consent prompts before enabling third-party plugin code.

### Configure the workspace

The viewer needs a FlowX workspace that contains `workflows/<workflow_id>/`.
Configure it in either place:

```bash
hermes config set plugins.entries.flowx.settings.workspace /absolute/path/to/flowx_workspaces
```

or in the gateway environment:

```bash
export FLOWX_WORKSPACE=/absolute/path/to/flowx_workspaces
```

`FLOWX_DEFAULT_WORKSPACE` is also accepted. The optional
`plugins.entries.flowx.settings.python` (or `FLOWX_PYTHON`) is displayed in the
viewer for reference; the viewer itself is stdlib-only and does not import the
FlowX SDK.

The viewer also accepts a single workflow directory (one containing
`plan/workflow.json`) as the configured workspace.

## Expected workflow layout

```text
<workspace>/workflows/<workflow_id>/
├── agent.json
├── manifest.json
├── plan/
│   ├── workflow.json
│   └── requirement_analysis.md
├── nodes/<node_id>/<version>/
├── service/service_<main_id>/main_entrypoint.py
├── logs/main_run.log
├── runs/
│   ├── latest.json
│   └── <run_id>/
│       ├── run.json
│       └── media/...
└── canvas_layout.json          # created by the viewer
```

## Safety and limits

- **Read-only.** No builds, runs, or workflow mutations. The only write is
  `canvas_layout.json` next to the workflow.
- **Path confinement.** Workflow ids and relative paths are validated; `..`,
  absolute paths, backslashes, dotfiles, secret-named paths, and symlinks are
  refused. Workflow folder symlinks are refused.
- **Bounded reads.** Text is limited to 1 MiB, JSON metadata to 8 MiB, media to
  200 MiB, and media responses to 8 MiB slices with `Range` support.
- **Redaction is best-effort.** Common API keys, bearer tokens, credential
  assignments, and credential-shaped JSON keys are redacted. Workspace content
  can still contain sensitive data. Do not expose the dashboard on an untrusted
  interface or to untrusted users.
- **No execution.** The viewer never imports workflow modules, runs node code,
  or decodes arbitrary binaries.

## Development

```bash
node --check plugins/hermes-flowx/dashboard/dist/index.js
/usr/local/bin/python3.10 -m pytest plugins/hermes-flowx/tests -q
hermes plugins validate plugins/hermes-flowx
```

Open `plugins/hermes-flowx/tests/viewer_demo.html` in a browser for a static
visual smoke test with mock workflow data (it loads React from a CDN and does not
need a running Hermes dashboard).

`hermes plugins validate` checks the manifest, runs `register()` in isolation,
compares declared tools with actual registrations, scans the code, and confirms
the plugin can run in Hermes's out-of-process plugin host.

## Compatibility

The plugin targets Hermes's documented dashboard plugin contract:

- `dashboard/manifest.json` with `entry`, `css`, and `api`;
- `window.__HERMES_PLUGIN_SDK__` and
  `window.__HERMES_PLUGINS__.register(name, Component)`;
- FastAPI `router` routes mounted under `/api/plugins/flowx/`.

It was validated with Hermes `vgit.466591f` (2026.9.24) using
`hermes plugins validate plugins/hermes-flowx`.

## License and provenance

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Keep the SPDX headers
and the FlowX origin identifier when redistributing or modifying these files.
