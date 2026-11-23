# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: cover begin_evolve (edit_node/edit_graph), revalidation, staging, and direct-edit integrity checks.
"""Contract-driven builds with a scripted stand-in for the host coding agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from builder_fixtures import (
    CHAIN, DIAMOND, REQUIREMENT, Agent, good, graph_of, node_code, node_test, plan_script,
)

from flowx_core.builder import BuildInProgress, BuildSettings, WorkflowBuilder, WorkflowFilesModified


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


def test_evolve_node_freezes_topology_and_revalidates_compatible_children(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)
    plan_before = (builder.record(workflow_id).graph_path).read_text(encoding="utf-8")

    opened = builder.begin_evolve(workflow_id, "change greeting text", scope="node", node="Greeter")
    assert opened["mode"] == "edit_node" and opened["baseline_version"] == 1
    assert [task["step_id"] for task in opened["next_tasks"]] == ["Greeter"]
    task = opened["next_tasks"][0]
    assert task["reason"] == "targeted"
    assert task["context"]["baseline"]["version"] == 1
    assert task["context"]["edit"] == {"mode": "edit_node", "baseline_version": 1, "topology_frozen": True}

    changed = {"code": node_code("Greeter", text="hey"), "test": node_test("Greeter", expected="hey")}
    reply = builder.submit_task(workflow_id, task["task_id"], changed)
    assert reply["accepted"] is True
    assert reply["next_tasks"] == []
    state = builder.get_build_state(workflow_id)
    assert state["status"] == "completed" and state["summary"]["workflow_version"] == 2
    assert {node["step_id"]: node["status"] for node in state["nodes"]} == {
        "Greeter": "published", "Shouter": "revalidated",
    }
    assert builder.record(workflow_id).graph_path.read_text(encoding="utf-8") == plan_before
    assert builder.verify_workflow(workflow_id)["ok"] is True


def test_evolve_node_reopens_broken_child_with_findings_and_parent_diff(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)

    opened = builder.begin_evolve(workflow_id, "rename derived key", scope="node", node="Greeter")
    greeter_task = opened["next_tasks"][0]
    renamed_code = node_code("Greeter", text="hey").replace('derived={"value": value}', 'derived={"greeting": value}')
    renamed_test = node_test("Greeter", expected="hey").replace('derived["value"]', 'derived["greeting"]')
    reply = builder.submit_task(workflow_id, greeter_task["task_id"], {"code": renamed_code, "test": renamed_test})
    assert reply["accepted"] is True

    # Shouter fails audit against Greeter's renamed derived key and opens with findings + parent source_diff.
    assert [t["step_id"] for t in reply["next_tasks"]] == ["Shouter"]
    shouter = builder.get_tasks(workflow_id)[0]
    assert shouter["reason"] == "upstream_changed"
    assert shouter["context"]["baseline"]["version"] == 1
    assert "node.dependency_results_derived_key_invalid" in {f["rule_id"] for f in shouter["context"]["last_findings"]}
    parent_changes = shouter["context"]["changes"]["parents"]
    assert len(parent_changes) == 1 and parent_changes[0]["name"] == "Greeter"
    assert parent_changes[0]["from_version"] == 1 and parent_changes[0]["to_version"] == 2
    assert '-        return StepRunOutput(card={"label": "Greeter", "value": value}, derived={"value": value})' in parent_changes[0]["source_diff"]
    assert '+        return StepRunOutput(card={"label": "Greeter", "value": value}, derived={"greeting": value})' in parent_changes[0]["source_diff"]

    fixed_shouter_code = node_code("Shouter", ["Greeter"]).replace('.derived["value"]', '.derived["greeting"]')
    fixed_shouter_test = node_test("Shouter", ["Greeter"]).replace('derived={"value": "Greeter-"}', 'derived={"greeting": "Greeter-"}')
    done = builder.submit_task(workflow_id, shouter["task_id"], {"code": fixed_shouter_code, "test": fixed_shouter_test})
    assert done["accepted"] is True
    assert builder.get_build_state(workflow_id)["status"] == "completed"


def test_evolve_graph_stages_plan_rejects_noop_and_aborts_cleanly(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)
    original_graph_text = builder.record(workflow_id).graph_path.read_text(encoding="utf-8")

    opened = builder.begin_evolve(workflow_id, "add Formatter node", scope="graph")
    assert opened["mode"] == "edit_graph" and opened["baseline_version"] == 1
    assert [task["stage"] for task in opened["next_tasks"]] == ["graph"]
    graph_task = opened["next_tasks"][0]
    assert graph_task["context"]["edit"] == {"mode": "edit_graph", "baseline_version": 1, "topology_frozen": False}

    # Identical graph is rejected as a no-op.
    noop = builder.submit_task(workflow_id, graph_task["task_id"], {"graph": CHAIN})
    assert noop["accepted"] is False
    assert [f["rule_id"] for f in noop["findings"]] == ["graph.edit_noop"]

    extended = graph_of(("Greeter", []), ("Shouter", ["Greeter"]), ("Formatter", ["Shouter"]))
    accepted = builder.submit_task(workflow_id, noop["next_tasks"][0]["task_id"], {"graph": extended})
    assert accepted["accepted"] is True
    assert accepted["published"]["staged"] is True
    assert accepted["published"]["diff"]["added"] == ["Formatter"]
    assert accepted["published"]["diff"]["unchanged"] == ["Greeter", "Shouter"]
    assert accepted["published"]["diff"]["rebuild"] == ["Formatter"]
    assert [task["step_id"] for task in accepted["next_tasks"]] == ["Formatter"]
    # Plan on disk stays untouched while the graph edit is still in flight.
    assert builder.record(workflow_id).graph_path.read_text(encoding="utf-8") == original_graph_text
    assert builder.get_build_state(workflow_id)["edit_impact"]["added"] == ["Formatter"]

    # Aborting leaves plan/ equal to the previous pinned snapshot.
    builder.abort_build(workflow_id, "cancel graph edit")
    assert builder.record(workflow_id).graph_path.read_text(encoding="utf-8") == original_graph_text
    assert builder.verify_workflow(workflow_id)["ok"] is True

    # Re-run and complete: plan/ is committed only after Formatter passes.
    reopened = builder.begin_evolve(workflow_id, "add Formatter node", scope="graph")
    g_reply = builder.submit_task(workflow_id, reopened["next_tasks"][0]["task_id"], {"graph": extended})
    f_reply = builder.submit_task(
        workflow_id, g_reply["next_tasks"][0]["task_id"], good("Formatter", ["Shouter"]),
    )
    assert f_reply["accepted"] is True
    final = builder.get_build_state(workflow_id)
    assert final["status"] == "completed" and final["summary"]["workflow_version"] == 2
    assert {n["step_id"]: n["status"] for n in final["nodes"]} == {
        "Greeter": "reused", "Shouter": "reused", "Formatter": "published",
    }
    assert [n["step_id"] for n in builder.list_nodes(workflow_id)] == ["Greeter", "Shouter", "Formatter"]
    assert builder.verify_workflow(workflow_id)["ok"] is True


def test_evolve_graph_modified_node_gets_spec_diff_and_baseline(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)
    modified_graph = graph_of(("Greeter", []), ("Shouter", ["Greeter"]))
    modified_graph["nodes"][1]["desc"] = "Shouter node with emphasis"

    opened = builder.begin_evolve(workflow_id, "update Shouter spec", scope="graph")
    reply = builder.submit_task(workflow_id, opened["next_tasks"][0]["task_id"], {"graph": modified_graph})
    assert reply["accepted"] is True
    assert [t["step_id"] for t in reply["next_tasks"]] == ["Shouter"]
    shouter = builder.get_tasks(workflow_id)[0]
    assert shouter["reason"] == "spec_changed"
    assert shouter["context"]["baseline"]["version"] == 1
    assert shouter["context"]["changes"]["spec"]["before"]["desc"] == "Shouter node"
    assert shouter["context"]["changes"]["spec"]["after"]["desc"] == "Shouter node with emphasis"


def test_evolve_requires_consistent_snapshot(builder: WorkflowBuilder) -> None:
    workflow_id = builder.create_workflow()["workflow_id"]
    with pytest.raises(ValueError, match="no consistent published snapshot"):
        builder.begin_evolve(workflow_id, "edit", scope="graph")


def test_integrity_blocks_direct_edits_to_plan_nodes_and_snapshots(builder: WorkflowBuilder) -> None:
    workflow_id, _ = build_chain(builder)
    record = builder.record(workflow_id)
    assert builder.verify_workflow(workflow_id)["ok"] is True
    assert builder.artifacts(workflow_id)["read_only"] is True
    graph_art = builder.read_artifact(workflow_id, "graph")
    assert graph_art["read_only"] is True and "Greeter" in graph_art["content"]

    # 1. Tamper with plan/workflow.json (inject a Rogue node).
    orig_graph = record.graph_path.read_text(encoding="utf-8")
    tampered = json.loads(orig_graph)
    tampered["nodes"].append({
        "name": "Rogue", "type": "Rogue", "meta_node_kind": "WorkflowStepNode",
        "desc": "injected", "ext_data": {"type": "none", "desc": "none"}, "depends": ["Shouter"],
    })
    record.graph_path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")

    report = builder.verify_workflow(workflow_id)
    assert report["ok"] is False
    assert any(item["path"] == "plan/workflow.json" for item in report["modified"])
    assert "Rogue" not in [node["step_id"] for node in builder.list_nodes(workflow_id)]
    cat = builder.catalog()[workflow_id]
    assert "Rogue" not in cat["nodes"] and "integrity" in cat["errors"]
    with pytest.raises(WorkflowFilesModified):
        builder.begin_build(workflow_id)
    with pytest.raises(WorkflowFilesModified):
        builder.begin_evolve(workflow_id, "x", scope="node", node="Greeter")
    with pytest.raises(WorkflowFilesModified):
        builder.read_artifact(workflow_id, "graph")
    record.graph_path.write_text(orig_graph, encoding="utf-8")

    # 2. Tamper with a published node source.
    node_src = Path(builder.artifacts(workflow_id)["nodes"]["Greeter"]["artifacts"]["source"])
    # Also locate the versioned node file under nodes/<id>/00000001/Greeter.py.
    latest_node = builder.store(workflow_id).latest_node("Greeter")
    assert latest_node is not None
    versioned_src = builder.store(workflow_id).artifacts_of(latest_node)["source"]
    orig_node = versioned_src.read_text(encoding="utf-8")
    versioned_src.write_text(orig_node + "\n# tampered\n", encoding="utf-8")
    assert builder.verify_workflow(workflow_id)["ok"] is False
    with pytest.raises(WorkflowFilesModified):
        builder.begin_build(workflow_id)
    with pytest.raises(WorkflowFilesModified):
        builder.read_artifact(workflow_id, "source", node="Greeter")
    versioned_src.write_text(orig_node, encoding="utf-8")

    # 3. Tamper with the workflow snapshot copy.
    orig_snap = node_src.read_text(encoding="utf-8")
    node_src.write_text(orig_snap + "\n# snapshot tampered\n", encoding="utf-8")
    assert builder.verify_workflow(workflow_id)["ok"] is False
    with pytest.raises(WorkflowFilesModified):
        builder.begin_build(workflow_id)
    node_src.write_text(orig_snap, encoding="utf-8")

    # 4. Tamper mid-build before submit_task.
    opened = builder.begin_evolve(workflow_id, "change greeting", scope="node", node="Greeter")
    task = opened["next_tasks"][0]
    record.graph_path.write_text(orig_graph + "\n", encoding="utf-8")
    rejected = builder.submit_task(workflow_id, task["task_id"], good("Greeter"))
    assert rejected["accepted"] is False
    assert rejected["error"] == "workflow_files_modified_outside_flowx"
    record.graph_path.write_text(orig_graph, encoding="utf-8")
    builder.abort_build(workflow_id)
    assert builder.verify_workflow(workflow_id)["ok"] is True
