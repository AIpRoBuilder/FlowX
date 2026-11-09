<!-- SPDX-FileCopyrightText: 2026 FlowX contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466 -->
<!-- Upstream: https://github.com/AIpRoBuilder/FlowX -->
<!-- Added 2026-11-09: document packaging, setup, operation, and safety. -->

# FlowX for DeepSeek Harness

This web-profile plugin adds an owner-only FlowX panel for building new
workflows, answering node-test repair prompts, and inspecting a workspace's
workflow graphs and artifacts. It is a UI and host integration, **not an agent
tool**. The separately installed [FlowX workflow skill](https://github.com/AIpRoBuilder/FlowX/tree/main/skills/flowx-workflows)
can guide an agent through additional build, inspection, and runtime operations.

## Prerequisites

- DeepSeek Harness `dsh` **0.2.2-rc.1**, Node.js **24.9.0 or newer**, and
  `pnpm` on `PATH` for the CLI plugin installer. This package declares an
  exact Harness version, not compatibility with other releases.
- Python **3.10 or newer** with the FlowX SDK and its dependencies installed in
  the interpreter used by the plugin. Installing this npm package does not
  install Python, the SDK, or the FlowX skill.
- A complete installation of the FlowX skill, **including its scripts**. The
  plugin invokes its `build.py` and `inspect_workflow.py`; copying only the
  skill instructions is insufficient.
- A provider and model with authorized API access. Export
  `FLOWX_LLM_API_KEY`, `FLOWX_LLM_PROVIDER`, and `FLOWX_LLM_MODEL` into the
  Harness host process environment before launch. The SDK and skill scripts do
  not automatically load a FlowX `.env` file; even inspection can require a
  provider key. Harness model settings do not automatically configure FlowX.

## Install and configure

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

dsh plugin --profile web add ./plugins/deepseek-harness-flowx
dsh web
```

The installer resolves the local path from the invoking directory and adds the
bundle to the web profile. Restart an already-running profile after installation
if necessary. The last command starts the web profile; open its reported URL
and select **FlowX** in the sidebar. `dsh plugin` manages a profile: it does
not start Harness or globally launch a process. For other installation methods,
see the [Harness CLI guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md).

`FLOWX_PYTHON` must be an absolute path to the executable interpreter with the
SDK installed; preserve the virtual-environment executable path rather than
resolving it to the base interpreter. Activating that environment also puts
its Python on `PATH` for the separate skill's runtime and smoke tests; check
that `dsh` and `pnpm` remain available after activation. `FLOWX_WORKSPACE`
must be an absolute path to an **existing directory**, used for FlowX artifacts
independently of the workspace selected in Harness. `FLOWX_SKILL_DIR` must be an absolute path
to the skill directory containing both scripts. The source-checkout layout
provides a default skill path when the plugin is loaded from that layout, but
an installed, copied, packed, or otherwise relocated distribution must set
`FLOWX_SKILL_DIR` explicitly to a separately installed skill. Set it explicitly
for linked installations too, so both linked and packed packages use the
intended skill. A local `add` and an explicit `link:` can have different
package-manager storage behavior; neither bundles the skill. Reinstall or
restart as appropriate after updating the plugin or skill.

The example's skill copy is separate from the plugin package. For another
project or user skill root, follow the
[FlowX skill installation instructions](https://github.com/AIpRoBuilder/FlowX#install-the-flowx-agent-skill)
and the Harness skill provider's discovery rules, then point
`FLOWX_SKILL_DIR` at that installed directory. Use the same absolute FlowX
workspace for the skill and the plugin. When redistributing the separate skill,
retain its attribution and applicable FlowX [LICENSE](LICENSE) and [NOTICE](NOTICE).
The plugin snapshots its environment at host initialization: restart the host
after changing settings. Desktop has a
separate plugin profile and may not inherit temporary exports from an unrelated
terminal; GUI launch environment and login-shell discovery vary by platform and
configuration. Arrange for the Desktop host to receive the same variables
securely before it starts; installing into the web profile alone does not
install into Desktop.

## Use

1. In **FlowX**, enter a workflow prompt, review the risk notice, give explicit
   consent, and select **Build workflow**. The plugin builds a new workflow;
   it does not automatically start its generated service.
2. The panel displays overall build status, but does **not** provide live
   per-stage progress or audit results. If a node test fails, review the repair
   prompt and optional test log, then provide guidance and select **Continue
   repair**. Empty guidance uses the test log; `test: ...` targets the test,
   and `skip` skips that amendment attempt. **Abort build** is available only
   while a repair prompt is paused, not throughout an active build.
3. After the build ends, select a workflow to inspect its current plan graph,
   dependencies, requirements, main entrypoint, and available node artifacts.
   The displayed graph is the **current plan**; the published workflow and node
   revisions or artifacts can differ after a graph-only update. Rebuild
   affected stages before relying on them as a consistent published workflow.
4. A skill build in the same workspace can be inspected here after it finishes:
   select **Refresh** under **Your workflows**, then select the workflow.
   There is no automatic synchronization with the skill or another process, and
   the plugin cannot answer or abort another process's paused build. Do not
   run two writers against the same workspace. The plugin's build/inspection
   coordination is local to this Node process; it does not lock other Harness,
   SDK, MCP, skill, or external processes.

The plugin has no workflow-runtime controls or automatic service launch. Review
generated code before running it. If the separately installed skill starts a
service, its own runtime process owns that service; this plugin neither manages
nor stops it. Follow the skill's runtime instructions and keep its process
alive as required.

## Security and privacy

Builds send prompts and potentially repair context to the configured model
provider. Generated tests and code run **without a sandbox** under the host
user's privileges and may access secrets, local files, and the network. Obtain
explicit consent for each build and its side effects; use only authorized
inputs and trusted environments. Auditing and tests do not establish safety.
The host filters some inherited environment variables and performs best-effort
redaction of displayed configuration and output; neither is a security boundary
or a guarantee that secrets are removed. Prompts, repair details, logs, and
artifacts can be sensitive even after redaction. Restrict workspace access and
review content before sharing it.

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