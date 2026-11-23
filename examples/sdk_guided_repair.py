# SPDX-License-Identifier: Apache-2.0
# Modified 2026-11-22: repair from structured findings; the agent fixes what FlowX reports, no LLM key is needed.
"""Build a FlowX workflow and repair a rejected node from its findings.

Setup (from the repository root):

    pip install -e .
    python examples/sdk_guided_repair.py

A rejected submission is not an error: ``submit_task`` returns ``accepted=False``, the ``findings`` (rule id,
message, line, remediation), the node's ``test`` result, and a repair task (``attempt`` + 1) whose
``context.last_findings`` and ``context.last_test_log`` repeat them. Fix exactly those findings and submit again;
omitted slots keep their previous content. After ``max_attempts`` the node is marked failed and its descendants
blocked. Here the scripted agent first gets a node wrong on purpose, then repairs it.
"""

from __future__ import annotations

from _demo_agent import INTENT, node_code, node_test, solve
from flowx_sdk import FlowXClient


def flawed_greeter() -> dict[str, str]:
    """A wrong STEP_ID and a test that expects the wrong value."""
    return {
        "code": node_code("Greeter", step_id="Hello"),
        "test": node_test("Greeter", expected="goodbye"),
    }


def main() -> None:
    with FlowXClient() as flowx:
        workflow_id = flowx.create_workflow()
        flowx.begin_build(workflow_id, INTENT)
        attempts = 0
        while flowx.get_build_state(workflow_id)["status"] == "running":
            task = flowx.get_tasks(workflow_id)[0]
            if task["step_id"] == "Greeter" and attempts == 0:
                artifacts, attempts = flawed_greeter(), 1
            elif task["step_id"] == "Greeter":
                artifacts = {"code": node_code("Greeter"), "test": node_test("Greeter")}
            else:
                artifacts = solve(task)
            response = flowx.submit_task(workflow_id, task["task_id"], artifacts)
            print(f"{task['task_id']}: accepted={response['accepted']}")
            for finding in response.get("findings", []):
                print(f"    [{finding['rule_id']}] {finding['message'][:100]!r} -> {finding['remediation']}")

        state = flowx.get_build_state(workflow_id)
        print(f"build {state['status']}; nodes: {[(node['step_id'], node['status']) for node in state['nodes']]}")


if __name__ == "__main__":
    main()
