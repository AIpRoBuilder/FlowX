import json
import tempfile
import unittest
from pathlib import Path

from flowx_core.architect.graph import Graph


class TestGraphGetAllSubgraph(unittest.TestCase):
    def test_get_all_subgraph_returns_root_to_each_node_json(self) -> None:
        graph_payload = {
            "nodes": [
                {"name": "A", "type": "A", "desc": "root"},
                {"name": "B", "type": "B", "desc": "b", "depends": ["A"]},
                {"name": "C", "type": "C", "desc": "c", "depends": ["A"]},
                {"name": "D", "type": "D", "desc": "d", "depends": ["B", "C"]},
            ]
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            graph_path = Path(tmp_dir) / "graph.json"
            graph_path.write_text(json.dumps(graph_payload), encoding="utf-8")

            graph = Graph(str(graph_path))
            subgraphs = graph.get_all_subgraph()

        self.assertEqual(set(subgraphs.keys()), {"A", "B", "C", "D"})

        self.assertEqual(
            subgraphs["A"],
            {"nodes": [{"name": "A", "type": "A", "desc": "root", "depends": []}]},
        )

        self.assertEqual(
            subgraphs["B"],
            {
                "nodes": [
                    {"name": "A", "type": "A", "desc": "root", "depends": []},
                    {"name": "B", "type": "B", "desc": "b", "depends": ["A"]},
                ]
            },
        )

        self.assertEqual(
            subgraphs["D"],
            {
                "nodes": [
                    {"name": "A", "type": "A", "desc": "root", "depends": []},
                    {"name": "B", "type": "B", "desc": "b", "depends": ["A"]},
                    {"name": "C", "type": "C", "desc": "c", "depends": ["A"]},
                    {"name": "D", "type": "D", "desc": "d", "depends": ["B", "C"]},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
