import json
import tempfile
from pathlib import Path

from flowx_core.architect.graph import Graph
from flowx_core.architect.graph_planner import GraphPlanner
from flowx_core.auditor.graph_json_auditor import GraphJsonAuditor


def _write_graph(payload: dict) -> Graph:
    with tempfile.TemporaryDirectory() as tmp_dir:
        graph_path = Path(tmp_dir) / "graph.json"
        graph_path.write_text(json.dumps(payload), encoding="utf-8")
        graph = Graph(str(graph_path))
        graph._temp_dir = tmp_dir
        return graph


def test_graph_json_auditor_accepts_graph_without_show_frontend() -> None:
    graph = _write_graph(
        {
            "nodes": [
                {"name": "VisibleStep", "type": "VisibleStep", "desc": "visible step"},
            ]
        }
    )

    is_valid, violations = GraphJsonAuditor().audit_graph_json(graph)

    assert is_valid
    assert violations == []


def test_graph_json_auditor_accepts_legacy_show_frontend_field() -> None:
    graph = _write_graph(
        {
            "nodes": [
                {
                    "name": "VisibleStep",
                    "type": "VisibleStep",
                    "desc": "visible step",
                    "show_frontend": True,
                },
                {
                    "name": "HiddenStep",
                    "type": "HiddenStep",
                    "desc": "hidden step",
                    "depends": ["VisibleStep"],
                },
            ]
        }
    )

    is_valid, violations = GraphJsonAuditor().audit_graph_json(graph)

    assert is_valid
    assert violations == []


def test_graph_planner_strips_legacy_show_frontend_field(tmp_path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "VisibleStep",
                        "type": "VisibleStep",
                        "desc": "visible step",
                        "show_frontend": "true",
                    },
                    {
                        "name": "HiddenStep",
                        "type": "HiddenStep",
                        "desc": "hidden step",
                        "show_frontend": "false",
                    },
                    {
                        "name": "DefaultStep",
                        "type": "DefaultStep",
                        "desc": "default step",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    planner = GraphPlanner(client=object())
    planner._normalize_ext_data_in_file(graph_path)
    normalized = json.loads(graph_path.read_text(encoding="utf-8"))

    assert "show_frontend" not in normalized["nodes"][0]
    assert "show_frontend" not in normalized["nodes"][1]
    assert "show_frontend" not in normalized["nodes"][2]


def test_graph_planner_backfills_meta_node_kind_and_keeps_operation_ext_type(tmp_path) -> None:
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "name": "CollectInput",
                        "type": "CollectInput",
                        "desc": "collect text",
                        "ext_data": {"type": "user_input", "desc": "collect text"},
                    },
                    {
                        "name": "FetchRemoteData",
                        "type": "FetchRemoteData",
                        "desc": "call remote api",
                        "ext_data": {"type": "url", "desc": "remote api"},
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    planner = GraphPlanner(client=object())
    planner._normalize_ext_data_in_file(graph_path)
    normalized = json.loads(graph_path.read_text(encoding="utf-8"))

    assert normalized["nodes"][0]["meta_node_kind"] == "WorkflowStepNode"
    assert normalized["nodes"][0]["ext_data"]["type"] == "user_input"
    assert normalized["nodes"][1]["meta_node_kind"] == "WorkflowOperationNode"
    assert normalized["nodes"][1]["ext_data"]["type"] == "url"

