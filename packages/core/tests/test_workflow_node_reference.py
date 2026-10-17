"""Modified: verify bytes file hooks, undecorated auditing, and output schemas."""

import pytest
from ag_ui_workflow import StepRunOutput
from ag_ui_workflow.nodes import WorkflowFileNode
from ag_ui_workflow.params import WorkflowParam

from flowx_core.architect.graph import NodeMeta
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools.file_tools import (
    compile_node_file_and_get_derived_keys,
    compile_node_file_and_get_step_output_card_schema,
)
from flowx_core.tools.workflow_node_reference import (
    render_workflow_method_signatures,
    render_workflow_step_meta_catalog,
    workflow_node_references,
)
import flowx_core.tools.workflow_node_reference as workflow_node_reference_module


def test_workflow_node_references_derive_traits_from_meta_node_kind() -> None:
    references = {reference.meta_node_kind: reference for reference in workflow_node_references()}

    assert set(references) == {
        "WorkflowStepNode",
        "WorkflowFileNode",
    }

    assert references["WorkflowStepNode"].step_meta.get("nodeKind") is None
    assert references["WorkflowStepNode"].capability_category == "input"
    assert references["WorkflowStepNode"].recommended_ext_data_type == "user_input"
    assert references["WorkflowStepNode"].supports_inputs_format is True
    assert references["WorkflowStepNode"].main_utility_methods == ("process_input",)
    assert references["WorkflowStepNode"].subclass_implementation_methods == ("process_input",)
    assert references["WorkflowStepNode"].step_output_schema_methods == ("process_input",)
    assert references["WorkflowStepNode"].planner_hooks == ("process_input",)

    assert references["WorkflowFileNode"].capability_category == "file"
    assert references["WorkflowFileNode"].recommended_ext_data_type == "user_file_input"
    assert references["WorkflowFileNode"].supports_inputs_format is False
    assert references["WorkflowFileNode"].main_utility_methods == ("process_input",)
    assert references["WorkflowFileNode"].subclass_implementation_methods == ("process_input",)
    assert references["WorkflowFileNode"].step_output_schema_methods == ("process_input",)
    assert references["WorkflowFileNode"].planner_hooks == ("process_input",)
    assert references["WorkflowFileNode"].base_class.__abstractmethods__ == frozenset({"process_input"})

    assert render_workflow_method_signatures(
        references["WorkflowStepNode"].base_class,
        references["WorkflowStepNode"].subclass_implementation_methods,
    ) == ("process_input(user_input, dependency_results, session_state)",)
    assert render_workflow_method_signatures(
        references["WorkflowFileNode"].base_class,
        references["WorkflowFileNode"].main_utility_methods,
    ) == ("process_input(file_bytes, dependency_results, session_state)",)


def test_workflow_node_references_prefer_ast_marked_hooks_over_runtime_helpers(monkeypatch) -> None:
    workflow_node_reference_module.workflow_node_references.cache_clear()

    class _FakeWorkflowTools:
        NODE_MAIN_UTILITY_SIGNATURE_ATTR = "__fake_main_utility__"
        NODE_SUBCLASS_IMPLEMENTATION_SIGNATURE_ATTR = "__fake_subclass_hook__"

        @staticmethod
        def get_node_main_utility_signature(_node_or_class):
            return "wrong_main_utility"

        @staticmethod
        def get_node_subclass_implementation_signatures(_node_or_class):
            return ["wrong_subclass_hook"]

    monkeypatch.setattr(
        workflow_node_reference_module,
        "_workflow_tools_module",
        lambda: _FakeWorkflowTools(),
    )

    references = {
        reference.meta_node_kind: reference
        for reference in workflow_node_reference_module.workflow_node_references()
    }

    assert references["WorkflowStepNode"].main_utility_methods == ("process_input",)
    assert references["WorkflowStepNode"].subclass_implementation_methods == ("process_input",)
    assert references["WorkflowFileNode"].main_utility_methods == ("process_input",)
    assert references["WorkflowFileNode"].subclass_implementation_methods == ("process_input",)

    workflow_node_reference_module.workflow_node_references.cache_clear()


def test_workflow_step_meta_catalog_uses_meta_node_kind_metadata() -> None:
    catalog = render_workflow_step_meta_catalog()

    assert "metaNodeKind: WorkflowStepNode" in catalog
    assert "capability category: input" in catalog
    assert "decorator-marked main utility methods: process_input" in catalog
    assert "StepRunOutput card/derived contract methods: process_input" in catalog
    assert "process_input(file_bytes, dependency_results, session_state)" in catalog
    assert "StepRunOutput card/derived contract methods: build_step_output" not in catalog
    assert "nodeKind:" not in catalog


def _file_example_source() -> str:
    from flowx_core.worker.node_writer import ROOT_DIR

    prompt = (ROOT_DIR / "worker/prompts/pydaograph_node_prompt.md").read_text(encoding="utf-8")
    return prompt.split("```python\n")[2].split("```", 1)[0]


@pytest.mark.parametrize("obsolete_output_hook", ["", "    def build_step_output(self, saved_files):\n        return saved_files\n"])
def test_file_node_missing_abstract_hook_is_rejected_by_runtime_and_auditor(tmp_path, obsolete_output_hook) -> None:
    class MissingFileNode(WorkflowFileNode):
        STEP_ID = "MissingFileNode"

    with pytest.raises(TypeError, match="abstract.*process_input"):
        MissingFileNode()

    path = tmp_path / "UploadNode.py"
    path.write_text(
        "from ag_ui_workflow.nodes import WorkflowFileNode\n"
        "class UploadNode(WorkflowFileNode):\n"
        "    STEP_ID = 'UploadNode'\n"
        "    DEPENDENCIES = []\n" + obsolete_output_hook,
        encoding="utf-8",
    )
    passed, violations = NodeAuditor().audit_file(str(path))
    assert not passed
    assert [violation.rule for violation in violations] == ["workflow_file_node_process_input_missing"]


def test_implemented_bytes_hook_passes_audit_and_preserves_output_schema(tmp_path) -> None:
    source = _file_example_source()
    path = tmp_path / "UploadNode.py"
    path.write_text(source, encoding="utf-8")
    meta = NodeMeta.from_dict({
        "name": "UploadNode", "type": "WorkflowFileNode", "desc": "Read text",
        "meta_node_kind": "WorkflowFileNode", "ext_data": {"type": "user_file_input"},
        "depends": [],
    })

    passed, violations = NodeAuditor().audit_file(str(path), node_meta=meta)
    assert passed, violations
    assert compile_node_file_and_get_derived_keys(str(path)) == ["byteCount", "text"]
    schema = compile_node_file_and_get_step_output_card_schema(str(path))
    assert schema is not None
    assert schema["step_id"] == "UploadNode"
    assert schema["card"]["label"] == "Uploaded text"
    assert set(schema["card"]) == {"label", "byteCount"}


@pytest.mark.parametrize("file_input, ready", [
    (None, False), ("text", False), ({"file_path": "/tmp/upload"}, False),
    ({"base64": ""}, False), ({"files": []}, False), ({"uploads": []}, False),
    ([], False), (b"", True), (bytearray(), True), (b"hello", True),
])
def test_file_prompt_gate_accepts_bytes_including_empty_not_legacy_formats(monkeypatch, file_input, ready) -> None:
    namespace = {}
    exec(compile(_file_example_source(), "<file-node-prompt>", "exec"), namespace)
    node = namespace["UploadNode"]()
    param = WorkflowParam()
    monkeypatch.setattr(node, "get_workflow_param", lambda: param)

    assert node.wait_function() is False
    param.inputs[node.STEP_ID] = file_input
    assert node.wait_function() is ready
    assert param.states[node.STEP_ID] == ("running" if ready else "awaiting_input")

    if ready:
        run_status = node.run()
        assert not run_status.isErr()
        output = param.outputs[node.STEP_ID]
        assert isinstance(output, StepRunOutput)
        assert output.card == {"label": "Uploaded text", "byteCount": len(file_input)}
        assert output.derived == {"text": bytes(file_input).decode("utf-8"), "byteCount": len(file_input)}
        assert node.wait_function() is True

    param.outputs.clear()
    node.DEPENDENCIES = ["FailedUpstream"]
    assert node.wait_function() is True


def test_file_runtime_rejects_non_step_output(monkeypatch) -> None:
    namespace = {}
    exec(compile(_file_example_source(), "<file-node-prompt>", "exec"), namespace)
    node = namespace["UploadNode"]()
    param = WorkflowParam()
    param.inputs[node.STEP_ID] = b"file content"
    monkeypatch.setattr(node, "get_workflow_param", lambda: param)

    def invalid_output(file_bytes, dependency_results, session_state):
        del dependency_results, session_state
        return {"derived": {"text": file_bytes.decode("utf-8")}}

    monkeypatch.setattr(node, "process_input", invalid_output)

    node.run()
    assert param.states[node.STEP_ID] == "failed"
    assert node.STEP_ID not in param.outputs
    assert param.failure is not None
    assert "process_input must return StepRunOutput" in param.failure.getInfo()


@pytest.mark.parametrize("legacy_helpers", [False, True])
def test_auditor_accepts_nodes_without_requiring_legacy_decorator_or_clone(tmp_path, legacy_helpers) -> None:
    source = _file_example_source()
    if legacy_helpers:
        source = source.replace("class UploadNode", "@register_class\nclass UploadNode")
        source += "\n    def clone(self):\n        return type(self)()\n"
    path = tmp_path / "UploadNode.py"
    path.write_text(source, encoding="utf-8")

    passed, violations = NodeAuditor().audit_file(str(path))
    assert passed, violations


@pytest.mark.parametrize("replacement, expected_rule", [
    (("    STEP_ID = \"UploadNode\"\n", ""), "step_id_missing"),
    (("STEP_ID = \"UploadNode\"", "STEP_ID = \"wrong\""), "step_id_mismatch"),
    (("DEPENDENCIES = []", "DEPENDENCIES = ['Unexpected']"), "dependencies_mismatch"),
    (("WorkflowFileNode", "WorkflowStepNode"), "meta_node_kind_requires_matching_base_class"),
])
def test_undecorated_nodes_keep_identity_and_metadata_validation(tmp_path, replacement, expected_rule) -> None:
    source = _file_example_source().replace(*replacement)
    path = tmp_path / "UploadNode.py"
    path.write_text(source, encoding="utf-8")
    meta = NodeMeta.from_dict({
        "name": "UploadNode", "type": "WorkflowFileNode", "desc": "Read text",
        "meta_node_kind": "WorkflowFileNode", "ext_data": {"type": "user_file_input"},
        "depends": [],
    })

    passed, violations = NodeAuditor().audit_file(str(path), node_meta=meta)
    assert not passed
    assert expected_rule in {violation.rule for violation in violations}


@pytest.mark.parametrize("base_name, hook_name, input_name", [
    ("WorkflowStepNode", "process_input", "user_input"),
    ("WorkflowFileNode", "process_input", "file_bytes"),
])
def test_auditor_checks_output_dependencies_for_undecorated_nodes(tmp_path, base_name, hook_name, input_name) -> None:
    source = (
        f"from ag_ui_workflow.nodes import {base_name}\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n"
        f"class Target({base_name}):\n"
        "    STEP_ID = 'Target'\n"
        "    DEPENDENCIES = ['Source']\n"
        f"    def {hook_name}(self, {input_name}, dependency_results, session_state):\n"
        "        return StepRunOutput(derived={'text': dependency_results['Source'].derived['missing']})\n"
    )
    (tmp_path / "Source.py").write_text(
        "from ag_ui_workflow.nodes import WorkflowStepNode\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n"
        "class Source(WorkflowStepNode):\n"
        "    STEP_ID = 'Source'\n"
        "    DEPENDENCIES = []\n"
        "    def process_input(self, user_input, dependency_results, session_state):\n"
        "        return StepRunOutput(derived={'text': user_input})\n",
        encoding="utf-8",
    )
    path = tmp_path / "Target.py"
    path.write_text(source, encoding="utf-8")

    passed, violations = NodeAuditor().audit_file(str(path))
    assert not passed
    assert [violation.rule for violation in violations] == ["dependency_results_derived_key_invalid"]

    path.write_text(source.replace("derived['missing']", "derived['text']"), encoding="utf-8")
    passed, violations = NodeAuditor().audit_file(str(path))
    assert passed, violations