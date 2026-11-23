# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-02: remove a hardcoded credential and clarify interactive repair.
# Modified 2026-11-22: the builder hands tasks to a coding agent; no LLM key is needed.
"""Build and run a FlowX workflow with the Python SDK.

Setup (from the repository root):

    pip install -e .
    python examples/sdk_quickstart.py

``FlowXClient.build(workflow_id, intent, solve)`` opens a build and answers every task FlowX sends with
``solve(task) -> artifacts``. Here ``solve`` is a scripted stand-in (see ``_demo_agent.py``); in a real
setup it calls your coding agent. Generated workflows are written under ``$FLOWX_DEFAULT_WORKSPACE``
(default ``./.flowx_workspaces``).
"""

from __future__ import annotations

import socket

from _demo_agent import INTENT, solve
from flowx_sdk import FlowXClient


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def main() -> None:
    with FlowXClient() as flowx:  # stops any service it started on exit
        workflow_id = flowx.create_workflow()

        # 1. Build. Each task is requirement -> graph -> one per node; FlowX audits and tests every artifact.
        state = flowx.build(workflow_id, INTENT, solve, fastapi_host="127.0.0.1", fastapi_port=free_port())
        print(f"build {state['status']}: {state['summary']}")
        for node in state["nodes"]:
            print(f"  {node['step_id']}: {node['status']} (version {node.get('version')})")
        if state["status"] != "completed":
            raise SystemExit(f"build failed: {state['error'] or state['errors']}")

        # 2. Inspect what was published.
        print("artifacts:", flowx.artifacts(workflow_id)["main_entrypoint"]["path"])

        # 3. Run it as a service and drive one step; run_service waits until the service answers.
        service = flowx.run_service(workflow_id)
        print(f"service running at {service['base_url']} (pid {service['pid']})")
        result = flowx.run_step(workflow_id, "Greeter", "")
        print("step status:", result["status"])

        # 4. Evolve: see sdk_evolve.py. Stopping happens automatically when the client closes.
        flowx.stop_service(workflow_id)


if __name__ == "__main__":
    main()
