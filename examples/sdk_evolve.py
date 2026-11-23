# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-22: evolve with begin_build(target=...) and agent-written artifacts; no LLM key is needed.
# Modified 2026-11-23: demonstrate begin_evolve(scope="node") with baseline diffs and automatic child revalidation.
"""Evolve an existing FlowX workflow: one node (frozen topology) or the graph.

Setup (from the repository root):

    pip install -e .
    python examples/sdk_evolve.py

``begin_evolve(workflow_id, intent, scope=...)`` takes the change to make as ``intent``:

* ``scope="node", node=<step id or node id>`` (see ``list_nodes``): rewrite only that node
  while graph topology stays frozen. With ``cascade="revalidate"`` (default), compatible
  descendants are re-tested and re-published automatically without opening a task; broken
  descendants open a task with findings and the parent ``source_diff``.
* ``scope="graph"`` (optional ``update_requirement=True``): edit ``workflow.json`` (staged
  until all nodes pass); unchanged nodes are reused, never rewritten.

The task's ``context.baseline`` and ``context.current`` hold the published files, so the
agent edits instead of starting over (never edit ``<workspace>/workflows/`` directly).
"""

from __future__ import annotations

from _demo_agent import INTENT, solve
from flowx_sdk import FlowXClient


def main() -> None:
    with FlowXClient() as flowx:
        workflow_id = flowx.create_workflow()
        flowx.build(workflow_id, INTENT, solve)
        print("v1:", [(node["step_id"], node["version"]) for node in flowx.list_nodes(workflow_id)])

        # Edit Greeter without changing graph topology.
        opened = flowx.begin_evolve(
            workflow_id, "Greet with 'hey' instead of 'hello'.", scope="node", node="Greeter",
        )
        task = opened["next_tasks"][0]
        print(
            f"mode={opened['mode']} baseline=v{opened['baseline_version']} "
            f"asked: {task['step_id']} ({task['reason']}), "
            f"baseline code has {len(task['context']['baseline']['code'])} chars"
        )

        louder = lambda current: solve(current, greeting="hey")  # noqa: E731
        state = flowx.get_build_state(workflow_id)
        while state["status"] == "running":
            task = flowx.get_tasks(workflow_id)[0]
            print(f"  answering {task['step_id']} ({task['reason']})")
            flowx.submit_task(workflow_id, task["task_id"], louder(task))
            state = flowx.get_build_state(workflow_id)

        print(f"build {state['status']}: workflow version {state['summary']['workflow_version']}")
        print("v2:", [(node["step_id"], node["version"]) for node in flowx.list_nodes(workflow_id)])


if __name__ == "__main__":
    main()
