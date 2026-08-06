# FlowX MCP Server

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

It follows the same `FastMCP` + stdio pattern used by the `hermes-agent` MCP server.

---

## Architecture

```
MCP client  ──stdio──►  flowx_mcp.server (FastMCP)
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
| `get_node_input_formats` | List every user-input node and the input it expects. |
| `run_workflow_step` | Format a chat message into a step input, run it, return results. |
| `list_workflows` | (helper) List workflows known to the server. |

## Setup

`meta_agent` and `ag_ui_workflow` must be importable. Install them editable:

```bash
python3.10 -m pip install -e /Users/xiechuxi/Desktop/codes/meta_agent --no-deps
python3.10 -m pip install -e /Users/xiechuxi/Desktop/codes/ag_ui_worflow --no-deps
python3.10 -m pip install -r requirements.txt
```

If you do not want to `pip install -e` them, set `FLOWX_EXTRA_PATHS` to a colon-separated
list of their source roots and the entry point will add them to `sys.path`.

Copy `.env.example` to `.env` and fill in your LLM key.

## Run

```bash
python3.10 run_server.py
# or with verbose logging
python3.10 run_server.py --verbose
```

## MCP client config (e.g. Claude Desktop / VS Code)

```jsonc
{
  "mcpServers": {
    "flowx": {
      "command": "python3.10",
      "args": ["/Users/xiechuxi/Desktop/codes/FlowX/run_server.py"],
      "env": {
        "FLOWX_LLM_PROVIDER": "deepseek",
        "FLOWX_LLM_MODEL": "deepseek-chat",
        "FLOWX_LLM_API_KEY": "<your key>",
        "FLOWX_DEFAULT_WORKSPACE": "/Users/xiechuxi/Desktop/codes/flowx_workspaces"
      }
    }
  }
}
```
