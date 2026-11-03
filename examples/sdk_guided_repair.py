# SPDX-License-Identifier: Apache-2.0
"""Build a FlowX workflow with user-guided node repair.

Setup (from the repository root):

    pip install -e .
    export FLOWX_LLM_PROVIDER=deepseek FLOWX_LLM_MODEL=deepseek-flash FLOWX_LLM_API_KEY=<your key>
    python examples/sdk_guided_repair.py

``build_nodes(...)`` pauses when a node's test fails (gating is on by default).
The result carries ``status="input_required"``, a ``request_id`` and ``awaiting``
(node, prompt, pytest log and test result). Answer with ``flowx.answer(...)``:
guidance amends the node, ``test: <guidance>`` amends the generated test instead,
``"skip"`` skips the amendment, and an empty string amends the node from the test
log alone.
"""

from __future__ import annotations

import sys

from flowx_sdk import FlowXClient

REQUIREMENT = (
    "Build a workflow that accepts a support ticket, classifies its category, "
    "assigns a priority, and drafts a reply."
)


def answer_prompt(awaiting: dict) -> str:
    """Prompt for guidance after one node's test fails."""
    print(f"\n=== {awaiting.get('node', 'node')} test failed ===")
    print(awaiting.get("prompt", ""))
    print((awaiting.get("test_log") or "")[-4000:])
    return input(
        "\nHow should I modify this node?\n"
        "  - enter a direction to guide the node amendment\n"
        "  - enter 'test: <direction>' to fix the generated test instead\n"
        "  - enter 'skip' to skip this amendment attempt\n"
        "  - press Enter to amend the node from the test log alone\n"
        "> "
    ).strip()


def main() -> None:
    with FlowXClient() as flowx:
        if not flowx.api_key:
            sys.exit("Set FLOWX_LLM_API_KEY (or pass api_key=...) before running this example.")

        # 1. Create workflow.json and its requirement analysis.
        created = flowx.build_workflow_json(REQUIREMENT).raise_for_failure()
        workflow_id = created.workflow_id
        print(f"workflow {workflow_id}: {created.artifacts['graph']}")

        # 2. Build the nodes. A failed node test pauses at that node's own gate (the
        # default); answer each question and continue until the build finishes.
        #
        # For non-interactive automation, pass await_repair_input=False: the engine
        # then amends from the test log without asking.
        built = flowx.build_nodes(
            workflow_id,
            await_repair_input=True,
            node_options={"max_repair_retries": 5},
        )
        asked = 0
        while built.status == "input_required":
            awaiting = built.awaiting
            asked += 1
            answer = answer_prompt(dict(awaiting))
            built = flowx.answer(built.request_id, answer, awaiting["stepId"])
        built.raise_for_failure()
        print(f"nodes: {', '.join(built.artifacts['nodes'])}")
        if asked:
            print(f"guided repair prompts: {asked}")
        else:
            print("No repair prompt was needed: every node test passed.")

        # 3. Generate the main entrypoint from the built nodes.
        entrypoint = flowx.build_main_entrypoint(workflow_id).raise_for_failure()
        print(f"entrypoint: {entrypoint.artifacts['main_entrypoint']['path']}")


if __name__ == "__main__":
    main()
