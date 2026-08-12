# FlowX MCP Server

FlowX is an MCP server for creating workflows through conversation, designed to integrate easily with agent systems such as Hermes, WorkBuddy, and TraeWork.

An [MCP](https://modelcontextprotocol.io) server that exposes the **`meta_agent`** workflow
builder and the **`ag_ui_workflow`** runtime engine as a set of tools, so any MCP client
(Claude Code, Cursor, Codex, VS Code Copilot Chat, ...) can:

1. **create** a new AG-UI workflow from a natural-language requirement,
2. **dynamically update** a workflow's backend artifacts (`workflow.json`, `{node}.py`,
   `main.py`) from a change prompt,
3. **start** the generated FastAPI backend engine for a workflow,
4. **reload** the backend after an update,
5. **inspect** the input format required by every user-input node,
6. **run** a workflow step from a chat message and collect the results.

It follows the same `FastMCP` tool-registration pattern used by the `hermes-agent`
MCP server. By default it serves over stdio, and it can also serve over
Streamable HTTP when you want clients to attach to an already-running instance.

---

## Architecture

```
MCP client  ──stdio / streamable-http──►  flowx_mcp.server (FastMCP)
                │
                ├── meta_agent.AgentBuilder  ──►  LLM (DeepSeek/...)
                │       (create / update / plan / generate)
                │
                └── subprocess: python main.py  ──►  FastAPI backend
                  │                                  │
                  │   POST /api/run-step (SSE)       │
                  └──────────────────────────────────┘
               ag_ui_workflow.WorkflowEngine
```

The MCP server keeps a per-workflow `WorkflowHandle` (an `AgentBuilder` plus the running
backend process, port and session state). Tools 3–6 talk to the running backend over HTTP
(`urllib`, no extra deps) and parse the AG-UI SSE event stream.

## Provided tools

| Tool | Purpose |
| --- | --- |
| `create_workflow` | Build a workflow from a requirement into `workspace/workflow_name`. |
| `update_workflow_node` | Amend `workflow.json` + `{node}.py` + `main.py` from a change prompt. |
| `start_backend` | Launch the generated FastAPI backend for a workflow. |
| `reload_workflow` | Restart the backend (picks up updated node files / `workflow.json`). |
| `restart_builder` | Recreate the in-memory `AgentBuilder` for a workflow from disk and optionally restart the backend. |
| `get_node_input_formats` | List every user-input node and the input it expects. |
| `run_workflow_step` | Format a chat message into a step input, run it, return results. |
| `list_workflows` | (helper) List workflows known to the server. |
| `list_workflow_folders` | List workflow folders discovered on disk under the workspace root. |
| `upload_workspace_input_file` | Save a base64-encoded file into `workspace/inputs` and return its path. |
| `list_workflow_python_files` | List all `.py` files under a workflow folder. |
| `get_workflow_files` | Read specific workflow files by file name or relative path. |
| `get_workflow_binary_files` | Read specific workflow binary files and return base64 content plus MIME type. |
| `replace_workflow_files` | Replace specific workflow files by file name or relative path. |

## Setup

`meta_agent` and `ag_ui_workflow` must be importable. Install them editable:

```bash
python3.10 -m pip install -e /Users/user/Desktop/codes/meta_agent --no-deps
python3.10 -m pip install -e /Users/user/Desktop/codes/ag_ui_worflow --no-deps
python3.10 -m pip install -r requirements.txt
```

If you do not want to `pip install -e` them, set `FLOWX_EXTRA_PATHS` to a colon-separated
list of their source roots and the entry point will add them to `sys.path`.

Copy `.env.example` to `.env` and fill in your LLM key.

## Run

```bash
# default: stdio transport for local MCP hosts
python3.10 run_server.py
# or with verbose logging
python3.10 run_server.py --verbose

# streamable HTTP transport for a long-running remote server
python3.10 run_server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

Use stdio when the MCP host should launch FlowX itself. Use Streamable HTTP when
you want to keep one FlowX instance running and let clients attach to it by URL.

## Local stdio MCP client config (e.g. Claude Desktop / VS Code)

```jsonc
{
  "mcpServers": {
    "flowx": {
      "command": "python3.10",
      "args": ["/Users/user/Desktop/codes/FlowX/run_server.py"],
      "env": {
        "FLOWX_LLM_PROVIDER": "deepseek",
        "FLOWX_LLM_MODEL": "deepseek-chat",
        "FLOWX_LLM_API_KEY": "<your key>",
        "FLOWX_DEFAULT_WORKSPACE": "/Users/user/Desktop/codes/flowx_workspaces"
      }
    }
  }
}
```

## Remote MCP client config for an existing FlowX server

If FlowX is already running on the remote host, start it there with the HTTP
transport and connect the client to the MCP endpoint instead of spawning a fresh
process over SSH.

Start the server on the remote machine:

```bash
cd /home/testuser/FlowX
export FLOWX_LLM_PROVIDER=deepseek
export FLOWX_LLM_MODEL=deepseek-chat
export FLOWX_LLM_API_KEY='<your key>'
export FLOWX_DEFAULT_WORKSPACE=/home/testuser/flowx_workspaces
export FLOWX_EXTRA_PATHS=/home/testuser/meta_agent:/home/testuser/ag_ui_worflow
python3 run_server.py --transport streamable-http --host 0.0.0.0 --port 8000 --verbose
```

Then point a URL-capable MCP host at that running server:

```jsonc
{
  "mcpServers": {
    "flowx-remote": {
      "url": "https://{url}/mcp"
    }
  }
}
```

If your MCP host only supports stdio launch commands, keep using the SSH pattern
below.

## Contact

If you are interested in using FlowX or co-building it, contact me at [peterxcx@gmail.com](mailto:peterxcx@gmail.com).

WeChat QR code:

![WeChat QR code placeholder](assets/qrcode.svg)

Replace this placeholder with the actual WeChat QR code image when it is available.
