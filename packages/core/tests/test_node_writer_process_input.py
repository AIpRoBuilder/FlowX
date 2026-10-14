"""Offline coverage for node writer dispatch and runtime context.

Modified: verify the explicit input-gate contract in generation and repair prompts.
Modified: verify bytes file hooks, explicit persistence, and optional legacy node helpers.
"""

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
    assert "self.setWaitFunc(self.wait_function)" in prompts[0]
    assert "INPUT_REQUIRED is UI metadata only" in prompts[0]


@pytest.mark.parametrize("coder_type", [WorkflowStepNodeCoder, WorkflowFileNodeCoder, WorkflowSkillNodeCoder])
def test_coder_includes_pooled_runtime_input_policy(coder_type) -> None:
    coder = coder_type(client=object())
    assert "self.setWaitFunc(self.wait_function)" in coder.system_prompt
    assert "param.condition" in coder.system_prompt
    assert "param.can_run(self.STEP_ID, self.DEPENDENCIES)" in coder.system_prompt
    assert "INPUT_REQUIRED is UI metadata only" in coder.system_prompt


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


@pytest.mark.parametrize("remote_desc", ["", "Store the upload in the configured remote object store"])
def test_file_coder_requires_bytes_processing_and_explicit_persistence(tmp_path, monkeypatch, remote_desc) -> None:
    coder = WorkflowFileNodeCoder(client=object())
    requirement = tmp_path / "requirement.md"
    requirement.write_text("Extract text from the uploaded file", encoding="utf-8")
    prompts = []

    def capture_code(prompt, path, **kwargs):
        del kwargs
        prompts.append(prompt)
        return Path(path)

    monkeypatch.setattr(coder, "code_to_file", capture_code)

    coder.process_input("", {}, {
        "WorkflowFileNodeCoder::action": "write",
        "node_writer": {
            "node_name": "UploadNode",
            "node_meta": {
                "name": "UploadNode", "type": "WorkflowFileNode", "desc": "Read uploaded text",
                "meta_node_kind": "WorkflowFileNode",
                "ext_data": {"type": "user_file_input", "remote_desc": remote_desc},
            },
            "requirement_md_path": str(requirement),
            "output_path": str(tmp_path / "UploadNode.py"),
        },
    })

    prompt = prompts[0]
    assert "Implement the abstract subclass hook process_input(file_bytes, dependency_results, session_state)" in prompt
    assert "file_bytes: bytes" in prompt
    assert "Implement local or remote persistence explicitly in process_input only when required" in prompt
    assert "isinstance(param.inputs[self.STEP_ID], (bytes, bytearray))" in prompt
    assert "empty bytes are valid" in prompt
    assert "Do not normalize file_path/base64/files/uploads in this subclass" in prompt
    assert "self.setWaitFunc(self.wait_function)" in prompt
    assert "saved_files" not in coder.get_node_contract_text()
    assert "StepRunOutput card/derived contract methods: build_step_output" not in prompt
    assert "no decorator-marked subclass implementation hook" not in prompt
    assert "Do not implement custom processing" not in prompt
    if remote_desc:
        assert f"remote_desc: {remote_desc}" in prompt
        assert "no inherited storage method exists to override" in prompt
        assert "only after storage succeeds" in prompt
    else:
        assert "WorkflowFileNode remote persistence rule" not in prompt


def test_file_coder_repair_preserves_bytes_hook_and_gate(tmp_path, monkeypatch) -> None:
    coder = WorkflowFileNodeCoder(client=object())
    output = tmp_path / "UploadNode.py"
    output.write_text("class UploadNode(WorkflowFileNode):\n    STEP_ID = 'UploadNode'\n", encoding="utf-8")
    prompts = []

    def capture_code(prompt, path, **kwargs):
        del kwargs
        prompts.append(prompt)
        return Path(path)

    monkeypatch.setattr(coder, "code_to_file", capture_code)

    coder.process_input("", {}, {
        "WorkflowFileNodeCoder::action": "amend",
        "node_writer": {"code_path": str(output), "amendment": "Implement the missing abstract file hook"},
    })

    assert "process_input(file_bytes, dependency_results, session_state)" in prompts[0]
    assert "file_bytes: bytes" in prompts[0]
    assert "isinstance(param.inputs[self.STEP_ID], (bytes, bytearray))" in prompts[0]
    assert "Preserve or implement requirement-defined persistence" in prompts[0]
    assert "Do not normalize file_path/base64/files/uploads in this subclass" in prompts[0]


@pytest.mark.parametrize("coder_type", [WorkflowStepNodeCoder, WorkflowFileNodeCoder, WorkflowSkillNodeCoder])
def test_node_prompts_do_not_require_global_registration_or_cloning(coder_type) -> None:
    coder = coder_type(client=object())
    assert "ordinary undecorated subclasses" in coder.system_prompt
    assert "JSON-node-name class maps" in coder.system_prompt
    assert "Optional legacy decorators/clone may remain" in coder.system_prompt
    assert "Decorate each step class" not in coder.system_prompt
    assert "from pydaograph import register_class" not in coder.system_prompt
    assert "and still define" not in coder.system_prompt