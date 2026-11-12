<!-- SPDX-FileCopyrightText: 2026 FlowX contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466 -->
<!-- Upstream: https://github.com/AIpRoBuilder/FlowX -->
<!-- Added 2026-11-09: document packaging, setup, operation, and safety. -->
<!-- Modified 2026-11-10: document the implemented read-only viewer, settings, and folder browsing. -->
<!-- Modified 2026-11-12: document Web and Desktop installation and configuration. -->

# FlowX for DeepSeek Harness

The plugin can be installed in DeepSeek Harness **Web or Desktop**. Desktop
uses the same Web client in its Electron shell; this is not a separate native
plugin. The interface is an owner-only, **read-only workflow viewer**. The
**FlowX** sidebar footer action opens a left-side overlay without replacing the
conversation; it overlays part of the window instead of adding a native,
resizable sidebar column. The overlay lists workflows, renders the current
plan graph with node descriptions, and browses the workflow directory. It is a
UI and host integration, **not an agent tool**. Use the separately installed
[FlowX workflow skill](https://github.com/AIpRoBuilder/FlowX/tree/main/skills/flowx-workflows)
to create, evolve, and run workflows.

The viewer intentionally has no **Start a build**, repair, or evolve controls.
The authenticated host API retains build endpoints for existing host
compatibility, but the browser interface does not surface them.

## Prerequisites

- DeepSeek Harness **0.2.2-rc.1**. This package declares an exact Harness
  version, not compatibility with other releases. The Web CLI requires
  Node.js **24.9.0 or newer** and `pnpm` on `PATH`. Desktop's installed
  command uses its bundled runtime and pnpm for plugin installation.
- Python **3.10 or newer** with the FlowX SDK and its dependencies installed in
  the interpreter used by the plugin. Installing this npm package does not
  install Python, the SDK, or the FlowX skill.
- The viewer and its settings need only the Python SDK. The FlowX skill is
  needed only by the retained build compatibility endpoints, which invoke its
  `build.py`.
- A provider and model with authorized API access. Configure
  `FLOWX_LLM_PROVIDER` and `FLOWX_LLM_MODEL` in the Harness host environment;
  provide `FLOWX_LLM_API_KEY` there or save it in the viewer settings. The key
  may be inherited by spawned skill processes. The SDK and skill scripts do not
  automatically load a FlowX `.env` file; even inspection can require a provider
  key. Harness model settings do not automatically configure FlowX.

## Install and configure

### Shared Python and skill setup

The following macOS/Linux example starts at a FlowX checkout root. Replace the
placeholder path, use a trusted checkout, and obtain the API key securely in
your environment; do not put it in a command, shell history, chat, or a
committed file. Create the virtual environment only if the chosen path is
unused, or use an existing Python 3.10+ environment with FlowX installed. Git
is needed to install FlowX's runtime dependency from its Git repository.

```sh
cd /absolute/path/to/FlowX
python3.10 -m venv .venv
.venv/bin/python -m pip install -e .
source .venv/bin/activate

# Install the complete skill where the Harness filesystem skill provider can find it.
# Review or back up an existing destination before copying over it.
mkdir -p .dsh/skills/flowx-workflows .flowx_workspaces
cp -R skills/flowx-workflows/. .dsh/skills/flowx-workflows/

export FLOWX_PYTHON="$PWD/.venv/bin/python"
export FLOWX_WORKSPACE="$PWD/.flowx_workspaces"
export FLOWX_SKILL_DIR="$PWD/.dsh/skills/flowx-workflows"
export FLOWX_LLM_PROVIDER=deepseek
export FLOWX_LLM_MODEL=deepseek-flash
# Set FLOWX_LLM_API_KEY securely in this environment before continuing.
: "${FLOWX_LLM_API_KEY:?Set FLOWX_LLM_API_KEY securely before launching Harness}"
export FLOWX_LLM_API_KEY
```

Then choose Web or Desktop below. The exported variables apply to this terminal
and its child processes; they are not automatically transferred to an already
running application or a Desktop app launched from the GUI.

### Web

From the same terminal and checkout root:

```sh
dsh plugin --profile web add ./plugins/deepseek-harness-flowx
dsh web
```

The installer resolves the local path from the invoking directory. Restart an
already-running Web host after installation if necessary, then open the URL
reported by `dsh web`. See the [Harness CLI guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/apps/cli/README.md).

### Desktop

1. Install and launch DeepSeek Harness Desktop **0.2.2-rc.1** once to initialize
  its Desktop profile. In the application menu, choose **Manage dsh Command…**
  and install the Desktop command. Open a new terminal, check `dsh --version`,
  and ensure the command resolves to the Desktop installation rather than an
  npm-installed CLI.
2. Fully quit Desktop, not just its window. From the FlowX checkout root, run:

  ```sh
  dsh plugin --profile desktop add ./plugins/deepseek-harness-flowx
  ```

3. Reopen Desktop. Select **FlowX** in the sidebar and use **Settings** to save
  the absolute Python and workspace paths and, if needed, the API key. Configure
  the provider and model in the Desktop host environment before launch.

Web and Desktop have separate plugin profiles: installing in one does not
install in the other. They share FlowX settings when they use the same
`DSH_HOME`. The npm-installed `dsh` cannot manage Desktop's profile;
the Desktop-installed command does not launch the app. Desktop's GUI launch may
not inherit exports from the terminal used above. On macOS and Linux Desktop
reads the login shell environment at startup; on Windows it inherits user and
system environment variables. Fully quit and reopen after changing host
environment settings. See the [Desktop guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.2-rc.1/apps/desktop/README.md#bundled-command-runtime)
for command installation and host environment details.

The skill copy above is project-local; ensure the Desktop agent's selected
workspace is that checkout, or install the skill in a Harness-discoverable user
skill root. If the retained build API needs a different skill location, set
`FLOWX_SKILL_DIR` in the Desktop host environment.

The Desktop installation path follows the Harness 0.2.2-rc.1 Desktop profile
and shared Web-client design; a real Desktop integration has not been validated
by the offline tests below.

### Viewer settings

Select **Settings** in the overlay to edit two existing absolute paths and the
API key. No skill directory is needed to save settings or view workflows; build
endpoints locate the skill on demand (`FLOWX_SKILL_DIR`, the source checkout,
then `$DSH_HOME/skills/flowx-workflows` or `~/.dsh/skills/flowx-workflows`,
then `./.dsh/skills/flowx-workflows`). The
dialog reloads the workflow list immediately after a successful save; changing
saved values does not require a host restart.

| Field | Environment fallback | Requirement |
| --- | --- | --- |
| `python` | `FLOWX_PYTHON` | Executable Python interpreter with the FlowX SDK installed. Preserve the virtual-environment executable path rather than resolving it to the base interpreter. |
| `workspace` | `FLOWX_WORKSPACE` | Existing directory for FlowX artifacts, independent of the workspace selected in Harness. |
| `apiKey` | `FLOWX_LLM_API_KEY` | Optional provider key. It is stored in the owner-only settings file, never returned to the browser, and an empty value keeps the saved key. |

The separately installed skill's scripts read this same settings file, so they
use the saved `python`, `workspace` and API key by default (explicit
`--workspace` still wins).

The host persists these paths in
`$DSH_HOME/plugins/deepseek-harness-flowx/settings.json`, or
`~/.dsh/plugins/deepseek-harness-flowx/settings.json` when `DSH_HOME` is unset.
The file is owner-only, mode `0600`; the plugin settings directory is mode
`0700`. `DSH_HOME`, when set, must be an absolute path; a symlinked `DSH_HOME`
base is resolved once, while links inside the plugin-managed paths remain
rejected. Saved settings override the corresponding environment fallbacks and
persist across restarts. Invalid or unsafe paths and storage are rejected
without overwriting the previous file, and the dialog shows the failure so it
can be corrected.

The host snapshots environment variables at initialization, so restart it after
changing environment fallbacks or provider credentials. The source-checkout
layout supplies a default skill path when available; for linked, installed,
copied, or packed plugins, explicitly configure the separately installed skill
path. Neither local `add` nor `link:` bundles the skill. Reinstall or restart as
appropriate after updating the plugin or skill. Activating the environment also
puts its Python on `PATH` for the separate skill's runtime and smoke tests;
check that `dsh` and `pnpm` remain available after activation.

The example's skill copy is separate from the plugin package. For another
project or user skill root, follow the
[FlowX skill installation instructions](https://github.com/AIpRoBuilder/FlowX#install-the-flowx-agent-skill)
and the Harness skill provider's discovery rules. If needed, point
`FLOWX_SKILL_DIR` at that installed directory for the host's build endpoints.
The skill reads the saved settings when it uses the same `DSH_HOME`; configure
its environment or workspace separately otherwise. When redistributing the
separate skill, retain its attribution and applicable FlowX [LICENSE](LICENSE)
and [NOTICE](NOTICE).

## Use

1. Select the **FlowX** sidebar footer action to open the left overlay. In
   narrow windows this dismisses Harness's navigation drawer first; the current
   conversation and its selection are preserved. Press **Escape**, select
   **Close**, or use the footer action again to close the overlay. Select
  **Settings** to configure `python`, `workspace`, and the API key.
2. Select a workflow to inspect its graph, dependencies, requirements, main
   entrypoint, and available node artifacts. The graph is the **current plan**,
   not necessarily the published revision: published workflow and node
   revisions or artifacts can differ after a graph-only update. Rebuild affected
   stages before relying on a consistent published workflow.
3. Expand the **Workflow folder** tree to browse the actual directory under
   `workspace/workflows/<workflow_id>` and open eligible files. Select
   **Refresh** after a separate skill operation finishes; there is no automatic
   synchronization with other processes.

The viewer has no build, repair, evolve, or runtime controls. Use the separate
skill for those operations, review generated code before running it, and follow
the skill's runtime instructions. A service started by the skill is owned by
its runtime process, not by the viewer.

### Folder browsing limits

Folder browsing omits hidden and credential-named paths, symlinks, and special
files; requests traversing symlinks are denied. Only regular, single-link
`.json`, `.md`, `.py`, `.txt`, and `.log`
files of at most 1 MiB that pass UTF-8 and safety checks are readable; other
files may appear but cannot be opened. Invalid JSON, JSON-shaped text that
cannot be safely parsed, and disallowed control characters are rejected. JSON
is structurally sanitized, and manifest error details and absolute entrypoint
paths are withheld.

Each folder listing returns at most 200 entries, scans at most 1,000 entries,
and is limited to 256 KiB of entry metadata and 4 MiB of text probes. A listing
can therefore be truncated or contain files that are not marked readable when
the probe budget is exhausted; it is not an exhaustive filesystem listing.

Host coordination of builds, inspections, and settings is local to this Node
process. It does **not** lock other Harness, SDK, MCP, skill, or external
processes, or prevent external workspace edits. Avoid concurrent writers to
the same workspace and inspect only after the other operation finishes.

## Security and privacy

Read-only describes the viewer UI, **not** the host's full capabilities: for
compatibility, its authenticated owner-only API still exposes build endpoints
even though the viewer has no build form. Do not expose the host route outside
Harness's authenticated owner-only boundary. Inspection can also create the
workspace's `workflows` storage directory if it is absent.

Builds through those endpoints or the separate skill send prompts and potentially
repair context to the configured model provider. Generated tests and code run
**without a sandbox** under the host user's privileges and may access secrets,
local files, and the network. Obtain explicit consent for each build and its
side effects; use only authorized inputs and trusted environments. Auditing and
tests do not establish safety. The host filters some inherited environment
variables and performs best-effort redaction of displayed configuration and
output; neither is a security boundary or a guarantee that secrets are removed.
Prompts, repair details, logs, and artifacts can be sensitive even after
redaction. Restrict workspace access and review content before sharing it.

## Tests and source

From the FlowX checkout root, with Node.js 24.9.0 or newer:

```sh
node --test plugins/deepseek-harness-flowx/tests/*.test.js
```

These offline unit tests use stubs and do not validate a real Harness or model
integration; the tests are not included in the packed plugin. The plugin entry
is [index.js](index.js), its browser interface is
[client.js](client.js), and the host implementation is
[host/service.js](host/service.js). The separate skill's
[scripts and instructions](https://github.com/AIpRoBuilder/FlowX/tree/main/skills/flowx-workflows)
are maintained upstream.

## License and attribution

Copyright 2026 FlowX contributors, for their respective contributions.
Original upstream: [AIpRoBuilder/FlowX](https://github.com/AIpRoBuilder/FlowX).
FlowX origin: `urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466`.
Distributed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE).