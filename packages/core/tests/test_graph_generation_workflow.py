"""Offline coverage for request-controlled graph generation without repair loops.

Modified 2026-10-14: exercise concrete pooled nodes, skips, and plan/amend modes.
Modified 2026-10-14: cover eager engines, terminal cleanup, and caller-owned state.
Modified 2026-10-16: run GraphGenerationWorkflow directly now that the service wrapper is gone.
"""

import json
from contextlib import ExitStack
from itertools import product
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import DEFAULT, Mock

import pytest
from ag_ui_workflow import RunResult, StepRunOutput, WorkflowEngine

from flowx_core.architect.graph import Graph
from flowx_core.architect.graph_planner import GraphPlanner
from flowx_core.auditor.graph_json_auditor import GraphJsonAuditor
from flowx_core.builder_support import BuildSession
from flowx_core.demand_analyzer.requirement_disector import RequirementDisector
from flowx_core.tools import workflow_engine as workflow_engine_module
from flowx_core.workflows.graph_generation import GraphGenerationRequest, GraphGenerationWorkflow


_GRAPH = {"nodes": [{
    "name": "Collect", "type": "Collect", "desc": "Collect inputs", "depends": [],
    "meta_node_kind": "WorkflowStepNode", "ext_data": {"type": "none", "desc": "none"},
}]}


class _Client:
    def __init__(self, content, label, events):
        self.response = content
        self.calls = []

        def create(**kwargs):
            events.append(label)
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.response))])

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _workflow(
    events, *, constructor=GraphGenerationWorkflow, analyzer_settings=None, planner_settings=None,
):
    analyzer = RequirementDisector(**{
        "client": _Client("# Generated analysis", "analyze", events),
        "model": "analysis-model", "use_streaming": False,
        **(analyzer_settings or {}),
    })
    planner = GraphPlanner(**{
        "client": _Client(json.dumps(_GRAPH), "planner", events),
        "model": "graph-model", "use_streaming": False,
        **(planner_settings or {}),
    })
    return constructor(analyzer=analyzer, planner=planner, graph_auditor=GraphJsonAuditor())


@pytest.fixture
def engine_spies(monkeypatch):
    real_run_steps = GraphGenerationWorkflow.run_steps
    run_spy = Mock()

    def run_steps(self, *args, **kwargs):
        run_spy(self, *args, **kwargs)
        return real_run_steps(self, *args, **kwargs)

    spies = SimpleNamespace(
        init=Mock(wraps=workflow_engine_module.build_workflow_pool),
        run=run_spy,
        close=Mock(wraps=workflow_engine_module.close_workflow_engine),
    )
    monkeypatch.setattr(workflow_engine_module, "build_workflow_pool", spies.init)
    monkeypatch.setattr(GraphGenerationWorkflow, "run_steps", run_steps)
    monkeypatch.setattr(workflow_engine_module, "close_workflow_engine", spies.close)
    return spies


@pytest.fixture
def workflow_factory(engine_spies):
    resources = []

    def create(events=None, **settings):
        resource = _workflow(events if events is not None else [], **settings)
        resources.append(resource)
        return resource

    yield create

    with ExitStack() as cleanup:
        for resource in resources:
            cleanup.callback(resource.close)
    assert engine_spies.close.call_count == engine_spies.init.call_count


@pytest.mark.parametrize("graph_action", ["plan", "amend"])
@pytest.mark.parametrize("skip_analysis, skip_plan, skip_audit", list(product((False, True), repeat=3)))
def test_native_graph_workflow_honors_every_skip_combination(
    tmp_path, monkeypatch, workflow_factory, engine_spies,
    graph_action, skip_analysis, skip_plan, skip_audit,
):
    events = []
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("# Existing analysis", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps(_GRAPH), encoding="utf-8")
    original_audit = GraphJsonAuditor.audit_graph_json

    def audit(self, graph):
        events.append("audit")
        assert graph.graph_json_path == graph_path
        return original_audit(self, graph)

    monkeypatch.setattr(GraphJsonAuditor, "audit_graph_json", audit)
    workflow = workflow_factory(events)
    engine = workflow
    assert engine is not None
    engine_spies.init.assert_called_once()
    engine_spies.run.assert_not_called()

    result = workflow.run(GraphGenerationRequest(
        requirement_text="Build a collection workflow", requirement_md_path=str(requirement_path),
        graph_plan_path=str(graph_path), graph_action=graph_action, amendment="Add validation",
        temperature=0.61, requirement_temperature=0.17,
        skip_analyze_requirement=skip_analysis, skip_plan_graph=skip_plan, skip_audit_graph=skip_audit,
    ))

    assert result.status == "completed"
    assert events == (
        ([] if skip_analysis else ["analyze"])
        + ([] if skip_plan else ["planner"])
        + ([] if skip_audit else ["audit"])
    )
    assert set(result.outputs) == {"analyze_requirement", "plan_graph", "audit_graph"}
    for step_id, skipped in (
        ("analyze_requirement", skip_analysis), ("plan_graph", skip_plan), ("audit_graph", skip_audit),
    ):
        if skipped:
            assert result.outputs[step_id].card == {"status": "skipped"}
    if not skip_analysis:
        assert requirement_path.read_text(encoding="utf-8") == "# Generated analysis"
        assert workflow.analyzer.client.calls[0]["model"] == "analysis-model"
        assert workflow.analyzer.client.calls[0]["temperature"] == 0.17
    if not skip_plan:
        call = workflow.planner.client.calls[0]
        assert call["model"] == "graph-model"
        assert call["temperature"] == 0.61
        prompt = call["messages"][-1]["content"]
        if graph_action == "plan":
            assert ("# Existing analysis" if skip_analysis else "# Generated analysis") in prompt
            assert "Generate graph_plan.json" in prompt
        else:
            assert "Add validation" in prompt
            assert "Existing graph plan:" in prompt
        assert graph_path.with_suffix(".mmd").read_text(encoding="utf-8").startswith("flowchart TD")
    else:
        assert not graph_path.with_suffix(".mmd").exists()
    engine_spies.init.assert_called_once()
    assert engine_spies.init.call_args.args[0] == {
        "analyze_requirement": RequirementDisector,
        "plan_graph": GraphPlanner,
        "audit_graph": GraphJsonAuditor,
    }
    engine_spies.run.assert_called_once()
    assert engine_spies.run.call_args.args == (engine,)
    assert workflow is engine
    engine_spies.close.assert_not_called()

    workflow.close()
    workflow.close()
    assert workflow.closed
    engine_spies.close.assert_called_once_with(engine)


def test_full_request_uses_actual_upstream_artifact_paths(
    tmp_path, monkeypatch, workflow_factory, engine_spies,
):
    workflow = workflow_factory()
    engine = workflow
    pool_close = Mock(wraps=engine.pool.close)
    monkeypatch.setattr(engine.pool, "close", pool_close)
    engine_spies.init.assert_called_once()
    engine_spies.run.assert_not_called()

    result = workflow.run(GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path=str(tmp_path / "analysis"),
        graph_plan_path=str(tmp_path / "graph"),
    ))

    assert isinstance(result, RunResult)
    assert result.status == "completed"
    assert result.outputs["analyze_requirement"].derived["requirement_md_path"] == str(tmp_path / "analysis.md")
    assert result.outputs["plan_graph"].derived["graph_plan_json_path"] == str(tmp_path / "graph.json")
    assert result.outputs["audit_graph"].derived["graph_plan_json_path"] == str(tmp_path / "graph.json")
    assert result.outputs["audit_graph"].derived["ok"] is True
    assert (tmp_path / "analysis.md").read_text(encoding="utf-8") == "# Generated analysis"
    graph = Graph(tmp_path / "graph.json")
    assert graph.get_topological_sorted_nodes() == ["Collect"]
    assert graph.nodes[0]["ext_data"] == {"type": "none", "desc": "no need for ext data"}
    assert graph.nodes[0]["loop"] == 1
    assert (tmp_path / "graph.mmd").is_file()
    assert not (tmp_path / "analysis").exists()
    assert not (tmp_path / "graph").exists()

    workflow.analyzer.client.response = "# Second analysis"
    second = workflow.run(GraphGenerationRequest(
        requirement_text="A separate request", requirement_md_path=str(tmp_path / "second_analysis.txt"),
        graph_plan_path=str(tmp_path / "second_graph.txt"),
    ))

    assert isinstance(second, RunResult)
    assert second.request_id != result.request_id
    assert second.outputs["analyze_requirement"].derived["requirement_md_path"] == str(tmp_path / "second_analysis.md")
    assert second.outputs["audit_graph"].derived["graph_plan_json_path"] == str(tmp_path / "second_graph.json")
    assert (tmp_path / "second_analysis.md").read_text(encoding="utf-8") == "# Second analysis"
    assert (tmp_path / "second_graph.mmd").is_file()
    assert (tmp_path / "analysis.md").read_text(encoding="utf-8") == "# Generated analysis"
    assert workflow is engine
    assert isinstance(workflow, WorkflowEngine)
    assert not any(isinstance(value, GraphGenerationRequest) for value in vars(workflow).values())
    engine_spies.init.assert_called_once()
    assert engine_spies.run.call_count == 2
    assert all(call.args == (engine,) for call in engine_spies.run.call_args_list)
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()

    workflow.close()
    workflow.close()
    assert workflow.closed
    engine_spies.close.assert_called_once_with(engine)
    pool_close.assert_called_once_with()
    with pytest.raises(RuntimeError, match="graph generation workflow is closed"):
        workflow.run(GraphGenerationRequest())
    engine_spies.init.assert_called_once()
    assert engine_spies.run.call_count == 2


@pytest.mark.parametrize("graph_action", ["plan", "amend"])
def test_tilde_paths_write_and_audit_the_same_artifact(
    tmp_path, monkeypatch, workflow_factory, graph_action,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    workflow = workflow_factory()
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_GRAPH), encoding="utf-8")

    result = workflow.run(GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path="~/analysis.md",
        graph_plan_path="~/graph.json", graph_action=graph_action, amendment="Add validation",
    ))

    assert result.outputs["analyze_requirement"].derived["requirement_md_path"] == str(tmp_path / "analysis.md")
    assert result.outputs["plan_graph"].derived["graph_plan_json_path"] == str(graph_path)
    assert result.outputs["audit_graph"].derived["graph_plan_json_path"] == str(graph_path)
    assert (tmp_path / "analysis.md").read_text(encoding="utf-8") == "# Generated analysis"
    assert graph_path.with_suffix(".mmd").is_file()


def test_audit_only_request_expands_existing_graph_path(tmp_path, monkeypatch, workflow_factory):
    monkeypatch.setenv("HOME", str(tmp_path))
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_GRAPH), encoding="utf-8")

    result = workflow_factory().run(GraphGenerationRequest(
        graph_plan_path="~/graph.json", skip_analyze_requirement=True, skip_plan_graph=True,
    ))

    assert result.outputs["audit_graph"].derived["graph_plan_json_path"] == str(graph_path)
    assert not graph_path.with_suffix(".mmd").exists()


@pytest.mark.parametrize("graph_action", ["plan", "amend"])
def test_graph_audit_failure_runs_once_without_repair(
    tmp_path, monkeypatch, workflow_factory, engine_spies, graph_action,
):
    events = []
    invalid = {"nodes": [{"name": "Collect", "type": "WrongType", "depends": []}]}
    requirement_path = tmp_path / "analysis.md"
    requirement_path.write_text("# Requirement", encoding="utf-8")
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(_GRAPH), encoding="utf-8")
    audit = Mock(wraps=GraphJsonAuditor.audit_graph_json)

    def audit_graph(self, graph):
        return audit(self, graph)

    monkeypatch.setattr(GraphJsonAuditor, "audit_graph_json", audit_graph)
    workflow = workflow_factory(events)
    engine = workflow
    pool_close = Mock(wraps=engine.pool.close)
    monkeypatch.setattr(engine.pool, "close", pool_close)
    planner_client = workflow.planner.client
    planner_client.response = json.dumps(invalid)
    request = GraphGenerationRequest(
        requirement_md_path=str(requirement_path), graph_plan_path=str(graph_path),
        graph_action=graph_action, amendment="Requested change", skip_analyze_requirement=True,
    )

    with pytest.raises(RuntimeError, match=f"graph {graph_action} audit did not pass") as error:
        workflow.run(request)

    assert "Line 1: name_type_mismatch" in str(error.value)
    assert events == ["planner"]
    assert len(planner_client.calls) == 1
    audit.assert_called_once()
    engine_spies.run.assert_called_once()
    assert workflow is engine
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()
    assert not graph_path.with_suffix(".mmd").exists()

    planner_client.response = json.dumps(_GRAPH)
    result = workflow.run(request)
    assert result.outputs["audit_graph"].derived["ok"] is True
    assert events == ["planner", "planner"]
    assert len(planner_client.calls) == 2
    assert audit.call_count == 2
    assert engine_spies.run.call_count == 2
    assert workflow is engine
    engine_spies.init.assert_called_once()
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()
    assert graph_path.with_suffix(".mmd").is_file()


def test_skipped_audit_does_not_reject_generated_graph(tmp_path, workflow_factory):
    workflow = workflow_factory()
    workflow.planner.client.response = json.dumps({"nodes": [
        {"name": "Collect", "type": "WrongType", "depends": []},
    ]})
    requirement_path = tmp_path / "analysis.md"
    requirement_path.write_text("# Requirement", encoding="utf-8")

    result = workflow.run(GraphGenerationRequest(
        requirement_md_path=str(requirement_path), graph_plan_path=str(tmp_path / "graph.json"),
        skip_analyze_requirement=True, skip_audit_graph=True,
    ))

    assert result.outputs["audit_graph"].card == {"status": "skipped"}
    assert (tmp_path / "graph.mmd").is_file()


@pytest.mark.parametrize("failed_step", ["analyze", "planner", "audit"])
def test_graph_workflow_step_failure_keeps_pool_open_and_recovers(
    tmp_path, monkeypatch, workflow_factory, engine_spies, failed_step,
):
    events = []
    node_class, method = {
        "analyze": (RequirementDisector, "analyze"),
        "planner": (GraphPlanner, "plan"),
        "audit": (GraphJsonAuditor, "audit_graph_json"),
    }[failed_step]
    original = getattr(node_class, method)
    attempts = 0

    def fail_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("deliberate step failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(node_class, method, fail_once)
    workflow = workflow_factory(events)
    engine = workflow
    pool_close = Mock(wraps=engine.pool.close)
    monkeypatch.setattr(engine.pool, "close", pool_close)
    request = GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path=str(tmp_path / "analysis.md"),
        graph_plan_path=str(tmp_path / "graph.json"),
    )

    with pytest.raises(RuntimeError, match="deliberate step failure"):
        workflow.run(request)

    assert events == {"analyze": [], "planner": ["analyze"], "audit": ["analyze", "planner"]}[failed_step]
    assert attempts == 1
    assert workflow is engine
    engine_spies.run.assert_called_once()
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()

    events.clear()
    result = workflow.run(request)
    assert result.status == "completed"
    assert result.outputs["audit_graph"].derived["ok"] is True
    assert events == ["analyze", "planner"]
    assert attempts == 2
    assert workflow is engine
    assert engine_spies.run.call_count == 2
    engine_spies.init.assert_called_once()
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()

    workflow.close()
    workflow.close()
    engine_spies.close.assert_called_once_with(engine)
    pool_close.assert_called_once_with()


def test_graph_workflow_run_error_keeps_engine_open_and_recovers(
    tmp_path, monkeypatch, workflow_factory, engine_spies,
):
    events = []
    workflow = workflow_factory(events)
    engine = workflow
    pool_close = Mock(wraps=engine.pool.close)
    monkeypatch.setattr(engine.pool, "close", pool_close)
    engine_spies.run.side_effect = [RuntimeError("deliberate run failure"), DEFAULT]
    request = GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path=str(tmp_path / "analysis.md"),
        graph_plan_path=str(tmp_path / "graph.json"),
    )

    with pytest.raises(RuntimeError, match="deliberate run failure"):
        workflow.run(request)

    assert events == []
    assert workflow is engine
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()
    assert workflow.run(request).status == "completed"
    assert events == ["analyze", "planner"]
    assert workflow is engine
    assert engine_spies.run.call_count == 2
    engine_spies.init.assert_called_once()
    engine_spies.close.assert_not_called()
    pool_close.assert_not_called()


def test_graph_workflow_preserves_custom_prompt_paths_before_node_initialization(
    tmp_path, workflow_factory,
):
    analyzer_prompt = tmp_path / "analyzer.md"
    planner_prompt = tmp_path / "planner.md"
    analyzer_prompt.write_text("CUSTOM ANALYZER", encoding="utf-8")
    planner_prompt.write_text("CUSTOM PLANNER", encoding="utf-8")
    workflow = workflow_factory(
        analyzer_settings={"prompt_path": str(analyzer_prompt)},
        planner_settings={"prompt_path": str(planner_prompt)},
    )

    workflow.run(GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path=str(tmp_path / "analysis.md"),
        graph_plan_path=str(tmp_path / "graph.json"),
    ))

    assert workflow.analyzer.client.calls[0]["messages"][0]["content"].startswith("CUSTOM ANALYZER")
    assert workflow.planner.client.calls[0]["messages"][0]["content"].startswith("CUSTOM PLANNER")


def test_graph_workflow_freezes_settings_but_shares_mutable_clients(tmp_path, workflow_factory):
    workflow = workflow_factory()
    analyzer_client = workflow.analyzer.client
    planner_client = workflow.planner.client
    workflow.analyzer.model = "changed-analysis-model"
    workflow.planner.model = "changed-graph-model"
    workflow.analyzer.system_prompt = "POST-CONSTRUCTION ANALYZER"
    workflow.planner.system_prompt = "POST-CONSTRUCTION PLANNER"
    workflow.planner.NONE_EXT_DESC = "post-construction description"
    analyzer_client.response = "# Updated analysis"

    workflow.run(GraphGenerationRequest(
        requirement_text="Collect data", requirement_md_path=str(tmp_path / "analysis.md"),
        graph_plan_path=str(tmp_path / "graph.json"),
    ))

    assert (tmp_path / "analysis.md").read_text(encoding="utf-8") == "# Updated analysis"
    assert analyzer_client.calls[0]["model"] == "analysis-model"
    assert planner_client.calls[0]["model"] == "graph-model"
    assert not analyzer_client.calls[0]["messages"][0]["content"].startswith("POST-CONSTRUCTION")
    assert not planner_client.calls[0]["messages"][0]["content"].startswith("POST-CONSTRUCTION")
    assert "# Updated analysis" in planner_client.calls[0]["messages"][-1]["content"]
    assert Graph(tmp_path / "graph.json").nodes[0]["ext_data"]["desc"] == "no need for ext data"


def test_workflow_seeds_native_state_by_identity_without_leaking_between_requests(
    tmp_path, monkeypatch, workflow_factory, engine_spies,
):
    sessions = {"first": BuildSession(), "second": BuildSession()}
    payloads = {label: {"label": label, "steps": []} for label in sessions}
    seeds = {
        "first": MappingProxyType({
            "build_session": sessions["first"], "payload": payloads["first"], "first_only": object(),
        }),
        "second": MappingProxyType({
            "build_session": sessions["second"], "payload": payloads["second"],
        }),
    }
    observed_states = {label: [] for label in sessions}

    def observe(node_class):
        original = node_class.process_input

        def process_input(self, user_input, dependency_results, state):
            label = state["payload"]["label"]
            assert state is not seeds[label]
            assert state["build_session"] is sessions[label]
            assert state["payload"] is payloads[label]
            if label == "second":
                assert "first_only" not in state
            if node_class is RequirementDisector:
                assert "step_marker" not in state
                state["step_marker"] = label
            else:
                assert state["step_marker"] == label
            state["payload"]["steps"].append(node_class.STEP_ID)
            observed_states[label].append(state)
            return original(self, user_input, dependency_results, state)

        monkeypatch.setattr(node_class, "process_input", process_input)

    for node_class in (RequirementDisector, GraphPlanner, GraphJsonAuditor):
        observe(node_class)
    workflow = workflow_factory()
    engine = workflow

    first = workflow.run(GraphGenerationRequest(
        requirement_text="First request", requirement_md_path=str(tmp_path / "first.md"),
        graph_plan_path=str(tmp_path / "first.json"), state=seeds["first"],
    ))
    workflow.analyzer.client.response = "# Second analysis"
    second = workflow.run(GraphGenerationRequest(
        requirement_text="Second request", requirement_md_path=str(tmp_path / "second.md"),
        graph_plan_path=str(tmp_path / "second.json"), state=seeds["second"],
    ))

    assert first.request_id != second.request_id
    for label, states in observed_states.items():
        assert len(states) == 3
        assert all(state is states[0] for state in states)
        assert payloads[label]["steps"] == ["analyze_requirement", "plan_graph", "audit_graph"]
        assert "step_marker" not in seeds[label]
        assert "requirement_disector" not in seeds[label]
    assert set(seeds["first"]) == {"build_session", "payload", "first_only"}
    assert set(seeds["second"]) == {"build_session", "payload"}
    assert first.outputs["analyze_requirement"].derived["requirement_md_path"] == str(tmp_path / "first.md")
    assert second.outputs["analyze_requirement"].derived["requirement_md_path"] == str(tmp_path / "second.md")
    assert (tmp_path / "first.md").read_text(encoding="utf-8") == "# Generated analysis"
    assert (tmp_path / "second.md").read_text(encoding="utf-8") == "# Second analysis"
    assert workflow is engine
    assert not any(isinstance(value, GraphGenerationRequest) for value in vars(workflow).values())
    engine_spies.init.assert_called_once()
    assert engine_spies.run.call_count == 2
    engine_spies.close.assert_not_called()


def test_graph_auditor_uses_upstream_path_and_serializes_violations(tmp_path):
    graph_path = tmp_path / "invalid.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect", "type": "WrongType"}]}), encoding="utf-8")

    result = GraphJsonAuditor().process_input("", {
        "plan_graph": StepRunOutput(derived={"graph_plan_json_path": str(graph_path)}),
    }, {"graph_auditor": {"graph_plan_json_path": "ignored.json"}})

    assert result.derived["graph_plan_json_path"] == str(graph_path)
    assert result.derived["ok"] is False
    assert result.derived["violations"][0]["rule"] == "name_type_mismatch"
    assert result.derived["violations"][0]["lineno"] == 1


@pytest.mark.parametrize("state, message", [
    ({}, "graph_auditor"),
    ({"graph_auditor": {}}, "graph_plan_json_path"),
])
def test_graph_auditor_rejects_incomplete_requests(state, message):
    with pytest.raises(ValueError, match=message):
        GraphJsonAuditor().process_input("", {}, state)


def test_graph_request_rejects_unknown_planner_action_before_engine_run(workflow_factory, engine_spies):
    workflow = workflow_factory()
    engine = workflow
    assert engine is not None
    engine_spies.init.assert_called_once()
    request = GraphGenerationRequest()
    object.__setattr__(request, "graph_action", "retry")

    with pytest.raises(ValueError, match="graph_action must be 'plan' or 'amend'"):
        workflow.run(request)

    engine_spies.run.assert_not_called()
    assert workflow is engine
    engine_spies.close.assert_not_called()
    assert workflow.run(GraphGenerationRequest(
        skip_analyze_requirement=True, skip_plan_graph=True, skip_audit_graph=True,
    )).status == "completed"
    engine_spies.init.assert_called_once()

def test_requirement_analysis_can_be_amended_before_the_graph_is_amended(tmp_path, workflow_factory, engine_spies):
    events = []
    requirement_path = tmp_path / "requirement.md"
    requirement_path.write_text("# Existing analysis", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps(_GRAPH), encoding="utf-8")
    workflow = workflow_factory(events)

    workflow.run(GraphGenerationRequest(
        requirement_md_path=str(requirement_path), graph_plan_path=str(graph_path),
        graph_action="amend", amendment="Add validation",
        requirement_action="amend", requirement_amendment="Validate every input",
    ))

    assert events == ["analyze", "planner"]
    prompt = workflow.analyzer.client.calls[0]["messages"][-1]["content"]
    assert "# Existing analysis" in prompt and "Validate every input" in prompt
    assert "Add validation" in workflow.planner.client.calls[0]["messages"][-1]["content"]

    request = GraphGenerationRequest()
    object.__setattr__(request, "requirement_action", "rewrite")
    engine_spies.run.reset_mock()
    with pytest.raises(ValueError, match="requirement_action must be 'write' or 'amend'"):
        workflow.run(request)
    engine_spies.run.assert_not_called()
