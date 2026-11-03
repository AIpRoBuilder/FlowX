# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-02: remove a hardcoded credential and clarify interactive repair.
"""Build, modify and run a FlowX workflow with the Python SDK.

Setup (from the repository root):

    pip install -e .
    export FLOWX_LLM_PROVIDER=deepseek FLOWX_LLM_MODEL=deepseek-flash FLOWX_LLM_API_KEY=<your key>
    python examples/sdk_quickstart.py

Generated workflows are written under ``$FLOWX_DEFAULT_WORKSPACE`` (default ``./.flowx_workspaces``).
Every build call returns an ``AgentResult``; ``raise_for_failure()`` turns a failed stage into an error.
"""

from __future__ import annotations

import sys

from flowx_sdk import FlowXClient

PORT = 8765
REQUIREMENT = (
    "生成一个上传文字生成表格让人工审核，最后生成pdf的工作流"
)


def main() -> None:
    with FlowXClient() as flowx:  # stops any service it started on exit
        if not flowx.api_key:
            sys.exit("Set FLOWX_LLM_API_KEY (or pass api_key=...) before running this example.")

        # 1. Create workflow.json and its requirement analysis. Nodes and main.py come later.
        created = flowx.build_workflow_json(REQUIREMENT).raise_for_failure()
        workflow_id = created.workflow_id
        print(f"workflow {workflow_id}: {created.artifacts['graph']}")

        # 2. Generate every node. A failing node test pauses for repair guidance;
        # an empty answer amends the node from its own log.
        built = flowx.build_nodes(workflow_id, await_repair_input=True)
        while built.status == "input_required":
            awaiting = dict(built.awaiting or {})
            print(f"=== {awaiting.get('node')} 测试失败 ===")
            print((awaiting.get("test_log") or "")[-4000:])
            answer = input("修复指引（回车=按日志自动修 / skip / test: 修测试）> ").strip()
            built = flowx.answer(built.request_id, answer, awaiting["stepId"])
        built.raise_for_failure()
        node_names = list(built.artifacts["nodes"])
        print(f"nodes: {', '.join(node_names)}")

        # 3. Generate the main entrypoint (the FastAPI service) from those nodes.
        entrypoint = flowx.build_main_entrypoint(workflow_id, fastapi_port=PORT).raise_for_failure()
        print(f"entrypoint: {entrypoint.artifacts['main_entrypoint']['path']}")

        # 4. Modify one node by step id; cascade regenerates main.py because the nodes changed.
        # last_node = node_names[-1]
        # flowx.build_nodes(
        #     workflow_id,
        #     {last_node: "Validate the input and raise a clear error when it is empty."},
        #     cascade=True,
        #     fastapi_port=PORT,
        # ).raise_for_failure()

        # 5. Evolve the whole workflow from one request; the engine decides which stages to rerun.
        # evolved = flowx.build_workflow(
        #     "Also return a one-line summary of the ticket.", workflow_id=workflow_id,
        #     fastapi_port=PORT, await_repair_input=False,
        # ).raise_for_failure()
        # print({name: stage.status for name, stage in evolved.stages.items()})

        # 6. Run it as a service; calling run_service again restarts it with the latest files.
        service = flowx.run_service(workflow_id)
        print(f"service running: pid {service['pid']}, listening on port {PORT}")
        input("Press Enter to stop the service... ")
        flowx.stop_service(workflow_id)


if __name__ == "__main__":
    main()



