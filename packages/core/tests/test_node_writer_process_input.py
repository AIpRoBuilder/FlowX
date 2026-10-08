"""Offline coverage for node writer workflow dispatch and runtime context."""

import json
from pathlib import Path

import pytest
from ag_ui_workflow import StepRunOutput

import flowx_core.worker.node_writer as node_writer
from flowx_core.worker.node_writer import (
    WorkflowFileNodeCoder,
    WorkflowSkillNodeCoder,
    WorkflowStepNodeCoder,
)


@pytest.mark.parametrize("coder_type", [WorkflowStepNodeCoder, WorkflowFileNodeCoder, WorkflowSkillNodeCoder])
def test_process_input_writes_with_all_live_context(tmp_path, monkeypatch, coder_type) -> None:
    coder = coder_type(client=object())
    requirement = tmp_path / "requirement.md"
    requirement.write_text("Generate the node", encoding="utf-8")
    output = tmp_path / "Target.py"
    prompts = []

    def capture_code(prompt, path, **kwargs):
        prompts.append(prompt)
        return Path(path)

    monkeypatch.setattr(coder, "code_to_file", capture_code)
    monkeypatch.setattr(node_writer, "_build_dependency_derived_context", lambda **kwargs: pytest.fail("file fallback"))
    monkeypatch.setattr(node_writer, "_build_ancestor_session_state_context", lambda **kwargs: pytest.fail("graph fallback"))
    request = {
        "node_writer": {
            "node_name": "Target",
            "node_meta": {"name": "Target", "type": "WorkflowStepNode", "desc": "test",
                          "depends": ["First", "Second"]},
            "requirement_md_path": str(requirement),
            "output_path": str(output),
            "graph_plan_path": str(tmp_path / "missing-graph.json"),
        },
        f"{coder_type.__name__}::action": "write",
        "session_id": "request-123",
        "shared": {"token": "persisted"},
    }
    results = {
        "First": StepRunOutput(derived={"one": 1, "nested": {"x": "y"}}),
        "Second": StepRunOutput(derived={"two": 2}),
    }

    result = coder.process_input("ignored", results, request)

    assert result.derived == {"generated_path": str(output)}
    assert '"one": 1' in prompts[0]
    assert '"nested": {"x": "y"}' in prompts[0]
    assert '"two": 2' in prompts[0]
    assert '"token": "persisted"' in prompts[0]
    assert '"session_id": "request-123"' in prompts[0]
    assert '"node_writer"' not in prompts[0]
    assert '::action"' not in prompts[0]
    assert "GraphContextBuilder" not in prompts[0]


def test_process_input_amends_with_fresh_runtime_context(tmp_path, monkeypatch) -> None:
    coder = WorkflowStepNodeCoder(client=object())
    output = tmp_path / "Target.py"
    output.write_text("class Target:\n    DEPENDENCIES = ['Old']\n", encoding="utf-8")
    prompts = []
    monkeypatch.setattr(coder, "code_to_file", lambda prompt, path, **kwargs: prompts.append(prompt) or Path(path))
    monkeypatch.setattr(node_writer, "_build_dependency_derived_context", lambda **kwargs: pytest.fail("file fallback"))
    monkeypatch.setattr(node_writer, "_build_ancestor_session_state_context", lambda **kwargs: pytest.fail("graph fallback"))

    result = coder.process_input("", {
        "Current": StepRunOutput(derived={"new_field": "live"}),
    }, {"WorkflowStepNodeCoder::action": "amend", "node_writer": {
        "code_path": str(output), "amendment": "Fix validation",
    }, "counter": 0})

    assert result.derived == {"generated_path": str(output)}
    assert "Fix validation" in prompts[0]
    assert '"new_field": "live"' in prompts[0]
    assert '"counter": 0' in prompts[0]
    assert "Old: (no derived keys detected)" not in prompts[0]
    assert "GraphContextBuilder" not in prompts[0]


def test_process_input_can_resolve_node_meta_from_graph(tmp_path, monkeypatch) -> None:
    coder = WorkflowStepNodeCoder(client=object())
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [{"name": "Target", "type": "WorkflowStepNode",
                                           "desc": "planned node"}]}), encoding="utf-8")
    requirement = tmp_path / "requirement.md"
    requirement.write_text("Generate", encoding="utf-8")
    prompts = []
    monkeypatch.setattr(coder, "code_to_file", lambda prompt, path, **kwargs: prompts.append(prompt) or Path(path))

    coder.process_input("", {}, {"WorkflowStepNodeCoder::action": "write", "node_writer": {
        "node_name": "Target", "requirement_md_path": str(requirement),
        "output_path": str(tmp_path / "Target.py"), "graph_plan_path": str(graph),
    }})

    assert "planned node" in prompts[0]


@pytest.mark.parametrize("session_state, message", [
    ({}, "node_writer"),
    ({"node_writer": {}}, "WorkflowStepNodeCoder::action"),
    ({"node_writer": {"action": "write"}}, "WorkflowStepNodeCoder::action"),
    ({"key_value": "write_code", "node_writer": {}}, "WorkflowStepNodeCoder::action"),
    ({"WorkflowStepNodeCoder::action": "plan", "node_writer": {}}, "WorkflowStepNodeCoder::action"),
    ({"WorkflowStepNodeCoder::action": "write", "node_writer": {}}, "node_name"),
    ({"WorkflowStepNodeCoder::action": "amend", "node_writer": {"code_path": "x.py"}}, "amendment"),
])
def test_process_input_rejects_invalid_request(session_state, message) -> None:
    with pytest.raises(ValueError, match=message):
        WorkflowStepNodeCoder(client=object()).process_input("", {}, session_state)