# SPDX-License-Identifier: Apache-2.0
"""Build, modify and run a FlowX workflow with the Python SDK.

Setup (from the repository root):

    pip install -e .
    export FLOWX_LLM_PROVIDER=deepseek FLOWX_LLM_MODEL=deepseek-chat FLOWX_LLM_API_KEY=<your key>
    python examples/sdk_quickstart.py

Generated workflows are written under ``$FLOWX_DEFAULT_WORKSPACE`` (default ``./.flowx_workspaces``).
Every build call returns an ``AgentResult``; ``raise_for_failure()`` turns a failed stage into an error.
"""

from __future__ import annotations

import sys

from flowx_sdk import FlowXClient

PORT = 8765
REQUIREMENT = (
    "Build a workflow that accepts a support ticket, classifies its category, assigns a priority, "
    "and drafts a reply. Return the category, priority and draft reply as structured output."
)


def main() -> None:
    with FlowXClient() as flowx:  # stops any service it started on exit
        flowx.api_key = "sk-70162c60a8ee4a8583224c1a8d6c4abb"
        if not flowx.api_key:
            sys.exit("Set FLOWX_LLM_API_KEY (or pass api_key=...) before running this example.")

        # 1. Create workflow.json and its requirement analysis. Nodes and main.py come later.
        created = flowx.build_workflow_json(REQUIREMENT).raise_for_failure()
        workflow_id = created.workflow_id
        print(f"workflow {workflow_id}: {created.artifacts['graph']}")

        # 2. Generate every node of the graph.
        built = flowx.build_nodes(workflow_id).raise_for_failure()
        node_names = list(built.artifacts["nodes"])
        print(f"nodes: {', '.join(node_names)}")

        # 3. Generate the main entrypoint (the FastAPI service) from those nodes.
        entrypoint = flowx.build_main_entrypoint(workflow_id, fastapi_port=PORT).raise_for_failure()
        print(f"entrypoint: {entrypoint.artifacts['main_entrypoint']['path']}")

        # 4. Modify one node by step id; cascade regenerates main.py because the nodes changed.
        last_node = node_names[-1]
        flowx.build_nodes(
            workflow_id,
            {last_node: "Validate the input and raise a clear error when it is empty."},
            cascade=True,
            fastapi_port=PORT,
        ).raise_for_failure()

        # 5. Evolve the whole workflow from one request; the engine decides which stages to rerun.
        evolved = flowx.build_workflow(
            "Also return a one-line summary of the ticket.", workflow_id=workflow_id, fastapi_port=PORT,
        ).raise_for_failure()
        print({name: stage.status for name, stage in evolved.stages.items()})

        # 6. Run it as a service; calling run_service again restarts it with the latest files.
        service = flowx.run_service(workflow_id)
        print(f"service running: pid {service['pid']}, listening on port {PORT}")
        input("Press Enter to stop the service... ")
        flowx.stop_service(workflow_id)


if __name__ == "__main__":
    main()
