import pytest
from ag_ui_workflow import StepRunOutput

from flowx_core.architect.graph_planner import GraphPlanner


@pytest.mark.parametrize("action", ["write", "amend"])
def test_graph_planner_process_input_dispatches_from_session_state(
    tmp_path, monkeypatch, action
) -> None:
    planner = GraphPlanner(client=object())
    output_path = tmp_path / "graph.json"
    calls = []

    def fake_plan(requirement_text, graph_path, **kwargs):
        calls.append(("plan", requirement_text, graph_path, kwargs))
        return graph_path

    def fake_amend(graph_path, amendment, **kwargs):
        calls.append(("amend", graph_path, amendment, kwargs))
        return graph_path

    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(planner, "amend_file_with_feedback", fake_amend)
    if action == "write":
        request = {
            "requirement_text": "Build a workflow",
            "output_path": str(output_path),
        }
        expected = ("plan", "Build a workflow", str(output_path))
    else:
        request = {
            "graph_json_path": str(output_path),
            "amendment": "Add a validation node",
        }
        expected = ("amend", str(output_path), "Add a validation node")

    result = planner.process_input("ignored", {}, {"GraphPlanner::action": action, "graph_planner": request})

    assert calls[0][: len(expected)] == expected
    assert isinstance(result, StepRunOutput)
    assert result.derived["graph_plan_json_path"] == str(output_path.resolve())


def test_graph_planner_process_input_accepts_requirement_file(tmp_path, monkeypatch) -> None:
    planner = GraphPlanner(client=object())
    requirement_path = tmp_path / "requirements.md"
    requirement_path.write_text("Build this workflow", encoding="utf-8")
    output_path = tmp_path / "graph.json"
    calls = []

    monkeypatch.setattr(
        planner,
        "plan",
        lambda text, path, **kwargs: calls.append((text, path)) or path,
    )

    planner.process_input(
        "",
        {},
        {
            "GraphPlanner::action": "write",
            "graph_planner": {
                "requirement_md_path": str(requirement_path),
                "output_path": str(output_path),
            }
        },
    )

    assert calls == [("Build this workflow", str(output_path))]


def test_graph_planner_process_input_rejects_missing_request() -> None:
    planner = GraphPlanner(client=object())

    with pytest.raises(ValueError, match="graph_planner"):
        planner.process_input("", {}, {})


def test_graph_planner_process_input_rejects_unknown_action() -> None:
    planner = GraphPlanner(client=object())

    with pytest.raises(ValueError, match="GraphPlanner::action must be 'write' or 'amend'"):
        planner.process_input("", {}, {"graph_planner": {"action": "write"}})
    with pytest.raises(ValueError, match="GraphPlanner::action must be 'write' or 'amend'"):
        planner.process_input("", {}, {"GraphPlanner::action": "plan", "graph_planner": {}})
