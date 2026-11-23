import json
import tempfile
from pathlib import Path

from flowx_core.architect.graph import Graph
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

