# SPDX-License-Identifier: Apache-2.0
"""Evolve an existing FlowX workflow: the whole workflow, one node, or let the engine decide.

Setup (from the repository root):

    pip install -e .
    export FLOWX_LLM_PROVIDER=deepseek FLOWX_LLM_MODEL=deepseek-flash FLOWX_LLM_API_KEY=<your key>
    python examples/sdk_evolve.py

``evolve(workflow_id, command, target=...)`` takes the change to make as ``command``:

* ``target="workflow"``: change the workflow itself (graph and requirement); nodes and the
  main entrypoint that depend on the change are rebuilt too.
* ``target=<step id or node id>`` (see ``list_nodes``): change only that node; the main
  entrypoint is regenerated because its nodes changed.
* no ``target``: the engine's judge reads the command and decides what it refers to.
"""

from __future__ import annotations

import sys

from flowx_sdk import WORKFLOW_TARGET, FlowXClient

REQUIREMENT = (
    "Build a workflow that accepts a support ticket, classifies its category, assigns a priority, "
    "and drafts a reply. Return the category, priority and draft reply as structured output."
)


def show(label: str, result) -> None:
    print(label, {name: stage.status for name, stage in result.stages.items()})


def main() -> None:
    with FlowXClient() as flowx:
        if not flowx.api_key:
            sys.exit("Set FLOWX_LLM_API_KEY (or pass api_key=...) before running this example.")

        # Build a complete workflow first: graph, nodes and main entrypoint in one request.
        built = flowx.build_workflow(REQUIREMENT).raise_for_failure()
        workflow_id = built.workflow_id

        # List the nodes: each has a step id (its name in workflow.json) and a stable node id.
        nodes = flowx.list_nodes(workflow_id)
        for node in nodes:
            print(f"{node['step_id']}: node_id={node['node_id']} version={node['version']}")

        # 1. Evolve the whole workflow: add or reshape nodes, change what the workflow does.
        evolved = flowx.evolve(
            workflow_id, "Add a step that detects the ticket language before classifying it.",
            target=WORKFLOW_TARGET,
        ).raise_for_failure()
        show("workflow:", evolved)

        # 2. Evolve one node, named by step id ...
        nodes = flowx.list_nodes(workflow_id)  # the workflow changed, so list again
        first = nodes[0]
        node_evolved = flowx.evolve(
            workflow_id, "Validate the input and raise a clear error when it is empty.",
            target=first["step_id"],
        ).raise_for_failure()
        show(f"node {first['step_id']}:", node_evolved)

        # ... or by node id.
        last = nodes[-1]
        flowx.evolve(
            workflow_id, "Keep the draft reply under 100 words.", target=last["node_id"],
        ).raise_for_failure()

        # 3. No target: the engine judges whether the command changes the workflow, a node or both.
        judged = flowx.evolve(workflow_id, "Also return a one-line summary of the ticket.")
        judged.raise_for_failure()
        print("judged intent:", judged.intent.to_dict() if judged.intent else None)

        # Evolve options are the same as for build_workflow, e.g. skip rebuilding later stages.
        flowx.evolve(
            workflow_id, "Tone down the reply.", target=last["step_id"], cascade=False,
        ).raise_for_failure()


if __name__ == "__main__":
    main()
    