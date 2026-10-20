# SPDX-License-Identifier: Apache-2.0
"""Build a FlowX workflow with user-guided node repair.

Setup (from the repository root):

    pip install -e .
    export FLOWX_LLM_PROVIDER=deepseek FLOWX_LLM_MODEL=deepseek-flash FLOWX_LLM_API_KEY=<your key>
    python examples/sdk_guided_repair.py

``repair_input_callback`` is called after a node's test fails. It receives a
``NodeRepairInput`` containing the node name, attempt, pytest log and test result.
Return guidance to combine with the log, ``"skip"`` to skip the amendment, or an
empty string to amend from the test log alone.
"""

from __future__ import annotations

import sys
from threading import Lock

from flowx_sdk import FlowXClient, NodeRepairInput

REQUIREMENT = (
    "Build a workflow that accepts a support ticket, classifies its category, "
    "assigns a priority, and drafts a reply."
)

_PROMPT_LOCK = Lock()
_PROMPTS: list[NodeRepairInput] = []


def ask_for_repair_input(context: NodeRepairInput) -> str:
    """Prompt for guidance after one node's test fails."""
    with _PROMPT_LOCK:
        _PROMPTS.append(context)
        print(
            f"\n=== {context.node_name} test failed (attempt {context.attempt + 1}) ==="
        )
        print(context.test_log[-4000:])
        answer = input(
            "\nHow should I modify this node?\n"
            "  - enter a direction to guide the amendment\n"
            "  - enter 'skip' to skip this amendment attempt\n"
            "  - press Enter to amend from the test log alone\n"
            "> "
        )
    return answer.strip()


def main() -> None:
    with FlowXClient() as flowx:
        if not flowx.api_key:
            sys.exit("Set FLOWX_LLM_API_KEY (or pass api_key=...) before running this example.")

        # 1. Create workflow.json and its requirement analysis.
        created = flowx.build_workflow_json(REQUIREMENT).raise_for_failure()
        workflow_id = created.workflow_id
        print(f"workflow {workflow_id}: {created.artifacts['graph']}")

        # 2. Build the nodes. After a node test fails, the callback asks for
        # amendment guidance; the node is retested and asked again until it passes
        # or max_repair_retries is exhausted.
        #
        # For non-interactive automation, use a deterministic callback instead, e.g.:
        #   repair_input_callback=lambda context: (
        #       "skip" if context.node_name == "DraftReply" else ""
        #   )
        built = flowx.build_nodes(
            workflow_id,
            repair_input_callback=ask_for_repair_input,
            node_options={"max_repair_retries": 5},
        ).raise_for_failure()
        print(f"nodes: {', '.join(built.artifacts['nodes'])}")
        if _PROMPTS:
            print(f"guided repair prompts: {len(_PROMPTS)}")
        else:
            print(
                "No repair prompt was needed: every node test passed, so there was "
                "nothing to amend. The callback runs only after a failing node test."
            )

        # 3. Generate the main entrypoint from the built nodes.
        entrypoint = flowx.build_main_entrypoint(workflow_id).raise_for_failure()
        print(f"entrypoint: {entrypoint.artifacts['main_entrypoint']['path']}")


if __name__ == "__main__":
    main()
