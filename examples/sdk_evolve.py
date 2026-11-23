# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-22: evolve with begin_build(target=...) and agent-written artifacts; no LLM key is needed.
"""Evolve an existing FlowX workflow: the whole workflow or one node.

Setup (from the repository root):

    pip install -e .
    python examples/sdk_evolve.py

``begin_build(workflow_id, intent, target=...)`` takes the change to make as ``intent``:

* ``target="workflow"``: redo the requirement and graph; nodes whose inputs changed are asked again.
* ``target=<step id or node id>`` (see ``list_nodes``): rewrite only that node. Its descendants are asked again
  because their upstream code changed; every other node is reused, never rewritten.
* no ``target``: build whatever is missing or stale.

The task's ``context.current`` holds the published files, so the agent edits instead of starting over.
"""

from __future__ import annotations

from _demo_agent import INTENT, solve
from flowx_sdk import FlowXClient


def main() -> None:
    with FlowXClient() as flowx:
        workflow_id = flowx.create_workflow()
        flowx.build(workflow_id, INTENT, solve)
        print("v1:", [(node["step_id"], node["version"]) for node in flowx.list_nodes(workflow_id)])

        # Rewrite Greeter. FlowX asks for it (reason "targeted"), then for Shouter ("upstream_changed").
        opened = flowx.begin_build(workflow_id, "Greet with 'hey' instead of 'hello'.", target="Greeter")
        task = opened["next_tasks"][0]
        print(f"asked: {task['step_id']} ({task['reason']}), current code has {len(task['context']['current']['code'])} chars")

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
