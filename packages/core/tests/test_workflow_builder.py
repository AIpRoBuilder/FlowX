# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Contract-driven builds with a scripted stand-in for the host coding agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from builder_fixtures import (
    CHAIN, DIAMOND, REQUIREMENT, Agent, good, graph_of, node_code, node_test, plan_script,
)

from flowx_core.builder import BuildInProgress, BuildSettings, WorkflowBuilder


@pytest.fixture
def builder(tmp_path: Path):
    instance = WorkflowBuilder(storage_dir=tmp_path / "workflows", settings=BuildSettings(max_attempts=3))
    yield instance
    instance.close()


def build_chain(builder: WorkflowBuilder) -> tuple[str, Agent]:
    workflow_id = builder.create_workflow(brief="Greet and shout")["workflow_id"]
    script = {**plan_script(CHAIN), "node:Greeter": [good("Greeter")], "node:Shouter": [good("Shouter", ["Greeter"])]}
    state = builder.begin_build(workflow_id, "Greet and shout")
    assert state["status"] == "running"
    assert [task["stage"] for task in state["next_tasks"]] == ["requirement"]
    agent = Agent(builder, workflow_id, script)
    agent.run()
    return workflow_id, agent


def test_full_build_publishes_nodes_and_entrypoint(builder: WorkflowBuilder) -> None:
    workflow_id, agent = build_chain(builder)

    assert agent.seen == ["requirement:requirement:a1", "graph:graph:a1", "node:Greeter:a1", "node:Shouter:a1"]
    state = builder.get_build_state(workflow_id)
    assert state["status"] == "completed", state
    assert state["next_action"] == {"kind": "run_service"}
    assert {stage["name"]: stage["status"] for stage in state["stages"]} == {
        "requirement": "done", "graph": "done", "nodes": "done", "entrypoint": "done",
    }
    assert {node["step_id"]: node["status"] for node in state["nodes"]} == {"Greeter": "published", "Shouter": "published"}
    assert state["summary"]["workflow_version"] == 1

    artifacts = builder.artifacts(workflow_id)
    assert artifacts["workflow_version"] == 1 and set(artifacts["nodes"]) == {"Greeter", "Shouter"}
    main = Path(artifacts["main_entrypoint"]["path"])
    assert main.name == "main.py" and (main.parent / "Greeter.py").is_file()
    assert [node["version"] for node in builder.list_nodes(workflow_id)] == [1, 1]

    events = builder.get_build_events(workflow_id)["events"]
    assert events[0]["type"] == "build_started" and events[-1]["type"] == "build_finished"
    assert {"task_issued", "task_closed", "node", "stage"} <= {event["type"] for event in events}
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)


def test_second_build_reuses_published_nodes(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)

    state = builder.begin_build(workflow_id)

    assert state["status"] == "completed" and state["next_tasks"] == []
    assert {node["step_id"]: node["status"] for node in state["nodes"]} == {"Greeter": "reused", "Shouter": "reused"}
    assert builder.artifacts(workflow_id)["workflow_version"] == 1
    assert [node["version"] for node in builder.list_nodes(workflow_id)] == [1, 1]


def test_audit_rejection_returns_findings_and_reissues_the_task(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    bad = {"code": node_code("Greeter", step_id="Wrong"), "test": node_test("Greeter")}
    script = {**plan_script(graph_of(("Greeter", []))), "node:Greeter": [bad, good("Greeter")]}
    builder.begin_build(workflow_id, "Greet")
    agent = Agent(builder, workflow_id, script)
    agent.run()

    rejected = agent.responses[2]
    assert rejected["accepted"] is False
    assert "node.step_id_mismatch" in {item["rule_id"] for item in rejected["findings"]}
    repair = rejected["next_tasks"][0]
    assert repair["attempt"] == 2 and repair["reason"] == "repair"
    assert repair["context"]["last_findings"] and repair["context"]["current"]["code"] == bad["code"]
    assert agent.responses[3]["accepted"] is True
    assert builder.get_build_state(workflow_id)["status"] == "completed"


def test_failing_test_is_reported_with_its_log(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    failing = {"code": node_code("Greeter"), "test": node_test("Greeter", fail=True)}
    script = {**plan_script(graph_of(("Greeter", []))), "node:Greeter": [failing, {"test": node_test("Greeter")}]}
    builder.begin_build(workflow_id, "Greet")
    agent = Agent(builder, workflow_id, script)
    agent.run()

    rejected = agent.responses[2]
    assert [item["rule_id"] for item in rejected["findings"]] == ["node.test_failed"]
    assert rejected["test"]["ok"] is False
    assert "Node Test Log" in rejected["next_tasks"][0]["context"]["last_test_log"]
    assert agent.responses[3]["accepted"] is True


def test_exhausted_attempts_fail_the_node_and_block_its_children(tmp_path: Path) -> None:
    builder = WorkflowBuilder(storage_dir=tmp_path / "w", settings=BuildSettings(max_attempts=1))
    try:
        workflow_id = builder.create_workflow()["workflow_id"]
        bad = {"code": "def broken(:\n", "test": node_test("Greeter")}
        script = {**plan_script(CHAIN), "node:Greeter": [bad]}
        builder.begin_build(workflow_id, "Greet")
        Agent(builder, workflow_id, script).run()

        state = builder.get_build_state(workflow_id)
        assert state["status"] == "failed" and state["next_action"]["kind"] == "escalate_to_user"
        assert {node["step_id"]: node["status"] for node in state["nodes"]} == {"Greeter": "failed", "Shouter": "blocked"}
        assert "nodes/Greeter" in state["errors"]
        assert builder.artifacts(workflow_id)["workflow_version"] == 0

        # A later build only asks for what is still missing.
        script = {"node:Greeter": [good("Greeter")], "node:Shouter": [good("Shouter", ["Greeter"])]}
        again = builder.begin_build(workflow_id)
        assert [task["step_id"] for task in again["next_tasks"]] == ["Greeter"]
        Agent(builder, workflow_id, script).run()
        assert builder.get_build_state(workflow_id)["status"] == "completed"
        assert builder.get_build_state(workflow_id)["errors"] == {}
    finally:
        builder.close()


def test_targeted_node_rebuild_dirties_descendants(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)

    state = builder.begin_build(workflow_id, "greet louder", target="Greeter")
    assert [task["step_id"] for task in state["next_tasks"]] == ["Greeter"]
    assert state["next_tasks"][0]["reason"] == "targeted"
    assert state["next_tasks"][0]["context"]["current"]["code"]
    changed = {
        "code": node_code("Greeter", text="hey"), "test": node_test("Greeter", expected="hey"),
    }
    agent = Agent(builder, workflow_id, {"node:Greeter": [changed], "node:Shouter": [good("Shouter", ["Greeter"])]})
    agent.step()

    shouter = builder.get_tasks(workflow_id)[0]
    assert shouter["step_id"] == "Shouter" and shouter["reason"] == "upstream_changed"
    agent.run()
    final = builder.get_build_state(workflow_id)
    assert final["status"] == "completed" and final["summary"]["workflow_version"] == 2
    assert [node["version"] for node in builder.list_nodes(workflow_id)] == [2, 2]


def test_unknown_target_and_missing_intent_are_rejected(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    with pytest.raises(ValueError, match="intent is required"):
        builder.begin_build(workflow_id)
    with pytest.raises(ValueError, match="unknown target"):
        builder.begin_build(workflow_id, "x", target="Nope")


def test_second_open_build_is_refused_and_stale_tasks_are_reported(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    first = builder.begin_build(workflow_id, "Greet")
    with pytest.raises(BuildInProgress):
        builder.begin_build(workflow_id, "again")

    stale = builder.submit_task(workflow_id, "requirement:requirement:r1:a9", {"requirement": REQUIREMENT})
    assert stale["accepted"] is False and stale["error"] == "stale_task"
    malformed = builder.submit_task(workflow_id, "nonsense", {})
    assert malformed["error"] == "malformed_task_id"
    assert builder.get_tasks(workflow_id)[0]["task_id"] == first["next_tasks"][0]["task_id"]


def test_requirement_and_graph_findings_are_structured(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    builder.begin_build(workflow_id, "Greet")

    bad = builder.submit_task(workflow_id, builder.get_tasks(workflow_id)[0]["task_id"], {"requirement": "# nothing"})
    assert {item["rule_id"] for item in bad["findings"]} == {"requirement.sections", "requirement.node_design"}
    assert all(item["remediation"] for item in bad["findings"])
    good_req = builder.submit_task(workflow_id, bad["next_tasks"][0]["task_id"], {"requirement": REQUIREMENT})
    assert good_req["accepted"] is True

    cyclic = graph_of(("A", ["B"]), ("B", ["A"]))
    rejected = builder.submit_task(workflow_id, good_req["next_tasks"][0]["task_id"], {"graph": cyclic})
    assert "graph.cycle_detected" in {item["rule_id"] for item in rejected["findings"]}
    broken = builder.submit_task(workflow_id, rejected["next_tasks"][0]["task_id"], {"graph": "{not json"})
    assert [item["rule_id"] for item in broken["findings"]] == ["graph.json"]


def test_sibling_gates_open_together_and_join_waits(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    script = {
        **plan_script(DIAMOND),
        "node:Left": [good("Left")], "node:Right": [good("Right")], "node:Join": [good("Join", ["Left", "Right"])],
    }
    builder.begin_build(workflow_id, "Diamond")
    agent = Agent(builder, workflow_id, script)
    agent.step()
    agent.step()

    tasks = builder.get_tasks(workflow_id)
    assert {task["step_id"] for task in tasks} == {"Left", "Right"}
    state = builder.get_build_state(workflow_id)
    assert {node["step_id"]: node["status"] for node in state["nodes"]}["Join"] == "blocked"

    agent.script = {key: value for key, value in script.items() if key.startswith("node:")}
    agent.step()
    remaining = builder.get_tasks(workflow_id)
    assert len(remaining) == 1 and remaining[0]["step_id"] in {"Left", "Right"}
    agent.run()
    assert builder.get_build_state(workflow_id)["status"] == "completed"
    join = builder.artifacts(workflow_id)["nodes"]["Join"]
    assert join["version"] == 1


def test_abort_keeps_published_nodes_and_allows_a_new_build(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    script = {**plan_script(CHAIN), "node:Greeter": [good("Greeter")]}
    builder.begin_build(workflow_id, "Greet")
    agent = Agent(builder, workflow_id, script)
    for _ in range(3):
        agent.step()
    assert [task["step_id"] for task in builder.get_tasks(workflow_id)] == ["Shouter"]

    state = builder.abort_build(workflow_id, "changed my mind")
    assert state["status"] == "aborted" and state["next_action"] == {"kind": "begin_build"}
    assert builder.get_tasks(workflow_id) == []
    assert builder.submit_task(workflow_id, "node:Shouter:r1:a1", {})["error"] == "no_active_build"

    resumed = builder.begin_build(workflow_id)
    assert [task["step_id"] for task in resumed["next_tasks"]] == ["Shouter"]
    assert {node["step_id"]: node["status"] for node in resumed["nodes"]}["Greeter"] == "reused"


def test_a_new_builder_resumes_from_disk(tmp_path: Path) -> None:
    first = WorkflowBuilder(storage_dir=tmp_path / "w")
    workflow_id = first.create_workflow()["workflow_id"]
    script = {**plan_script(CHAIN), "node:Greeter": [good("Greeter")]}
    first.begin_build(workflow_id, "Greet")
    agent = Agent(first, workflow_id, script)
    for _ in range(3):
        agent.step()

    # The first process dies without closing: its build never wrote a finish event.
    second = WorkflowBuilder(storage_dir=tmp_path / "w")
    try:
        assert workflow_id in second.workflow_ids
        assert second.get_build_state(workflow_id)["status"] == "interrupted"
        state = second.begin_build(workflow_id)
        assert [task["step_id"] for task in state["next_tasks"]] == ["Shouter"]
        assert state["revision"] == 2
        Agent(second, workflow_id, {"node:Shouter": [good("Shouter", ["Greeter"])]}).run()
        assert second.get_build_state(workflow_id)["status"] == "completed"
    finally:
        second.close()
        first.close()


def test_unsafe_workflow_names_are_rejected(builder: WorkflowBuilder) -> None:
    for name in ("..", "a/b", "CON", "trailing."):
        with pytest.raises(ValueError):
            builder.create_workflow(name)
    assert builder.create_workflow("demo")["workflow_id"] == "demo"
    with pytest.raises(FileExistsError):
        builder.create_workflow("demo")
    assert json.loads((builder.storage_dir / "demo" / "agent.json").read_text())["workflow_id"] == "demo"
