"""Modified 2026-10-13: metadata must reflect generated pooled node contracts."""
# Modified 2026-10-14: pass graph, mapping, and path callbacks explicitly.

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowx_core.architect.graph import Graph
from flowx_core.builder_services import build_steps_meta


def _builder(tmp_path: Path, nodes: list[dict]) -> SimpleNamespace:
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    graph = Graph(str(path))
    return SimpleNamespace(
        node_location_map={},
        _load_planned_graph=lambda: graph,
        _resolve_root_path=lambda value: tmp_path / value,
        _expected_backend_node_path=lambda name: tmp_path / f"{name}.py",
    )


def _metadata(builder, **kwargs):
    return build_steps_meta(
        builder._load_planned_graph(), node_location_map=builder.node_location_map,
        resolve_root_path=builder._resolve_root_path,
        expected_backend_node_path=builder._expected_backend_node_path, **kwargs,
    )


def test_metadata_resolves_automatic_input_and_disabled_nodes_without_files(tmp_path):
    builder = _builder(tmp_path, [
        {"name": "Collect", "meta_node_kind": "WorkflowStepNode", "ext_data": {"type": "user_input"}},
        {"name": "Automatic", "meta_node_kind": "WorkflowStepNode", "ext_data": {"type": "none"}},
        {"name": "Upload", "meta_node_kind": "WorkflowFileNode", "ext_data": {"type": "user_file_input"}},
        {"name": "Skill", "meta_node_kind": "WorkflowSkillNode", "ext_data": {"type": "skill"}},
        {"name": "Disabled", "meta_node_kind": "WorkflowStepNode", "enable": False},
    ])
    metadata = {step["id"]: step for step in _metadata(builder)}
    assert "Disabled" not in metadata
    assert {name: step["inputRequired"] for name, step in metadata.items()} == {
        "Collect": True, "Automatic": False, "Upload": True, "Skill": False,
    }
    hidden = {step["id"]: step for step in _metadata(builder, include_hidden_nodes=True)}
    assert hidden["Disabled"]["enabled"] is False


def test_metadata_reads_generated_constants_without_executing_source(tmp_path):
    builder = _builder(tmp_path, [{
        "name": "Skill", "meta_node_kind": "WorkflowSkillNode", "desc": "Planned prompt",
    }])
    builder.node_location_map["Skill"] = "custom.py"
    (tmp_path / "custom.py").write_text('''raise AssertionError("Do not import generated modules")
class Skill(WorkflowSkillNode):
    STEP_ID = "actual-skill"
    TITLE = "Actual title"
    PROMPT = ""
    DEPENDENCIES: list[str] = ["actual-parent"]
    INPUT_REQUIRED = True
''', encoding="utf-8")
    metadata = _metadata(builder)[0]
    assert metadata["id"] == "actual-skill"
    assert metadata["title"] == "Actual title"
    assert metadata["prompt"] == ""
    assert metadata["dependencies"] == ["actual-parent"]
    assert metadata["inputRequired"] is True


@pytest.mark.parametrize("source", [
    "class Other: INPUT_REQUIRED = True\n",
    "invalid python !!!",
    "class Skill: INPUT_REQUIRED = configure_input()\n",
    "class Skill: INPUT_REQUIRED = 'False'\n",
])
def test_metadata_keeps_plan_defaults_for_unresolved_constants(tmp_path, source):
    builder = _builder(tmp_path, [{"name": "Skill", "meta_node_kind": "WorkflowSkillNode"}])
    (tmp_path / "Skill.py").write_text(source, encoding="utf-8")
    assert _metadata(builder)[0]["inputRequired"] is False