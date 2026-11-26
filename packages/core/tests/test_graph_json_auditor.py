import json
import tempfile
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("node,expected_valid", [
    ({"name": "Ask", "type": "Ask", "meta_node_kind": "WorkflowStepNode", "desc": "ask",
      "ext_data": {"type": "user_input", "desc": "question"}, "inputs_format": {"name": "string", "count": "integer"}}, True),
    ({"name": "Ask", "type": "Ask", "meta_node_kind": "WorkflowStepNode", "desc": "ask",
      "ext_data": {"type": "user_input", "desc": "question"}}, False),
    ({"name": "Ask", "type": "Ask", "meta_node_kind": "WorkflowStepNode", "desc": "ask",
      "ext_data": {"type": "user_input", "desc": "question"}, "inputs_format": {"ticket": "text"}}, False),
    ({"name": "Upload", "type": "Upload", "meta_node_kind": "WorkflowFileNode", "desc": "upload",
      "ext_data": {"type": "user_file_input", "desc": "file"}, "inputs_format": {"file": "string"}}, False),
])
def test_graph_json_auditor_validates_inputs_format(node: dict, expected_valid: bool) -> None:
    is_valid, violations = GraphJsonAuditor().audit_graph_json(_write_graph({"nodes": [node]}))

    assert is_valid is expected_valid
    assert (violations == []) if expected_valid else any(v.rule == "inputs_format" for v in violations)

