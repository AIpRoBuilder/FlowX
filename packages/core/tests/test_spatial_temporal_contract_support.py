from flowx_core.architect.graph import NodeMeta
from flowx_core.auditor.node_auditor import NodeAuditor
from flowx_core.tools.file_tools import (
    compile_node_file_and_get_derived_keys,
    compile_node_file_and_get_step_output_card_schema,
)


def _write_spatial_temporal_node(node_file_path):
    node_file_path.write_text(
        "from pydaograph import register_class\n"
        "from ag_ui_workflow.nodes import SpatialTemporalContractNode\n\n"
        "@register_class\n"
        "class BuildContract(SpatialTemporalContractNode):\n"
        "    STEP_ID = \"BuildContract\"\n"
        "    TITLE = \"Build Contract\"\n"
        "    PROMPT = \"Generate contract\"\n"
        "    DEPENDENCIES = [\"DescribeScene\"]\n"
        "\n"
        "    def clone(self):\n"
        "        return self\n",
        encoding="utf-8",
    )


def _write_file_node(node_file_path):
    node_file_path.write_text(
        "from pydaograph import register_class\n"
        "from ag_ui_workflow.nodes import WorkflowFileNode\n\n"
        "@register_class\n"
        "class UploadFiles(WorkflowFileNode):\n"
        "    STEP_ID = \"UploadFiles\"\n"
        "    TITLE = \"Upload Files\"\n"
        "    PROMPT = \"Upload files\"\n"
        "    DEPENDENCIES = []\n"
        "\n"
        "    def clone(self):\n"
        "        return self\n",
        encoding="utf-8",
    )


def test_node_auditor_accepts_spatial_temporal_contract_node(tmp_path):
    node_file_path = tmp_path / "BuildContract.py"
    _write_spatial_temporal_node(node_file_path)

    ok, violations = NodeAuditor().audit_node_file(
        str(node_file_path),
        node_meta=NodeMeta(
            name="BuildContract",
            type="BuildContract",
            desc="Generate a spatial-temporal contract",
            ext_data={
                "type": "spatial_temporal_contract",
                "desc": "build contract json from upstream description",
            },
            depends=["DescribeScene"],
        ),
    )

    assert ok is True
    assert violations == []


def test_file_tools_fallback_to_spatial_temporal_contract_base_methods(tmp_path):
    node_file_path = tmp_path / "BuildContract.py"
    _write_spatial_temporal_node(node_file_path)

    derived_keys = compile_node_file_and_get_derived_keys(str(node_file_path))
    assert "spatialTemporalContract" in derived_keys
    assert "spatialTemporalContractJson" in derived_keys
    assert "objectCount" in derived_keys
    assert "relationCount" in derived_keys

    card_schema = compile_node_file_and_get_step_output_card_schema(str(node_file_path))
    assert card_schema is not None
    assert card_schema["step_id"] == "BuildContract"
    assert card_schema["card"]["title"] == "<expr:boolop>"
    assert "response" in card_schema["card"]
    assert "contract" in card_schema["card"]
    assert "model" in card_schema["card"]


def test_file_tools_fallback_to_file_node_base_build_step_output(tmp_path):
    node_file_path = tmp_path / "UploadFiles.py"
    _write_file_node(node_file_path)

    card_schema = compile_node_file_and_get_step_output_card_schema(str(node_file_path))

    assert card_schema is not None
    assert card_schema["step_id"] == "UploadFiles"
    assert "fileCount" in card_schema["card"]
    assert "files" in card_schema["card"]


def test_node_auditor_checks_overridden_spatial_temporal_process_operation(tmp_path):
    node_file_path = tmp_path / "BuildContract.py"
    node_file_path.write_text(
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        "from pydaograph import register_class\n"
        "from ag_ui_workflow.nodes import SpatialTemporalContractNode\n"
        "from ag_ui_workflow.workflow_types import StepRunOutput\n\n"
        "@register_class\n"
        "class BuildContract(SpatialTemporalContractNode):\n"
        "    STEP_ID = \"BuildContract\"\n"
        "    TITLE = \"Build Contract\"\n"
        "    PROMPT = \"Generate contract\"\n"
        "    DEPENDENCIES = [\"DescribeScene\"]\n\n"
        "    def clone(self):\n"
        "        return self\n\n"
        "    def process_operation(\n"
        "        self,\n"
        "        dependency_results: dict[str, StepRunOutput],\n"
        "        session_state: dict[str, Any],\n"
        "    ) -> StepRunOutput:\n"
        "        return StepRunOutput(card={\"title\": self.TITLE}, derived={})\n",
        encoding="utf-8",
    )

    ok, violations = NodeAuditor().audit_node_file(
        str(node_file_path),
        node_meta=NodeMeta(
            name="BuildContract",
            type="BuildContract",
            desc="Generate a spatial-temporal contract",
            ext_data={
                "type": "spatial_temporal_contract",
                "desc": "build contract json from upstream description",
            },
            depends=["DescribeScene"],
        ),
    )

    assert ok is False
    assert any(v.rule == "dependency_results_missing_dependency_keys" for v in violations)