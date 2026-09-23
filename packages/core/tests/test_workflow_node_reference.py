from flowx_core.tools.workflow_node_reference import (
    render_subclass_guidance_method_signatures,
    render_workflow_method_signatures,
    render_workflow_step_meta_catalog,
    workflow_node_references,
)
import flowx_core.tools.workflow_node_reference as workflow_node_reference_module


def test_workflow_node_references_derive_traits_from_meta_node_kind() -> None:
    references = {reference.meta_node_kind: reference for reference in workflow_node_references()}

    assert references["WorkflowStepNode"].step_meta.get("nodeKind") is None
    assert references["WorkflowStepNode"].capability_category == "input"
    assert references["WorkflowStepNode"].recommended_ext_data_type == "user_input"
    assert references["WorkflowStepNode"].supports_inputs_format is True
    assert references["WorkflowStepNode"].main_utility_methods == ("process_input",)
    assert references["WorkflowStepNode"].subclass_implementation_methods == ("process_input",)
    assert references["WorkflowStepNode"].step_output_schema_methods == ("process_input",)
    assert references["WorkflowStepNode"].planner_hooks == ("process_input",)

    assert references["WorkflowOperationNode"].capability_category == "operation"
    assert references["WorkflowOperationNode"].recommended_ext_data_type == "none"
    assert references["WorkflowOperationNode"].main_utility_methods == ("process_operation",)
    assert references["WorkflowOperationNode"].subclass_implementation_methods == ("process_operation",)
    assert references["WorkflowOperationNode"].step_output_schema_methods == ("process_operation",)
    assert references["WorkflowOperationNode"].planner_hooks == ("process_operation",)

    assert references["WorkflowFileNode"].capability_category == "file"
    assert references["WorkflowFileNode"].recommended_ext_data_type == "user_file_input"
    assert references["WorkflowFileNode"].main_utility_methods == ("save_files",)
    assert references["WorkflowFileNode"].subclass_implementation_methods == ()
    assert references["WorkflowFileNode"].step_output_schema_methods == ("build_step_output",)
    assert references["WorkflowFileNode"].planner_hooks == ()

    assert references["WorkflowSkillNode"].capability_category == "skill"
    assert references["WorkflowSkillNode"].recommended_ext_data_type == "skill"
    assert references["WorkflowSkillNode"].supports_inputs_format is True
    assert references["WorkflowSkillNode"].main_utility_methods == ("process_operation",)
    assert references["WorkflowSkillNode"].subclass_implementation_methods == ("process_operation",)
    assert references["WorkflowSkillNode"].step_output_schema_methods == ("process_operation",)
    assert references["WorkflowSkillNode"].planner_hooks == ("process_operation",)

    spatial_reference = references["SpatialTemporalContractNode"]
    spatial_hook_signatures = render_workflow_method_signatures(
        spatial_reference.base_class,
        spatial_reference.subclass_implementation_methods,
    )
    spatial_guidance_signatures = render_subclass_guidance_method_signatures(
        spatial_reference.base_class,
        spatial_reference.subclass_implementation_methods,
    )

    assert references["SpatialTemporalContractNode"].capability_category == "spatial_temporal_contract"
    assert references["SpatialTemporalContractNode"].recommended_ext_data_type == "spatial_temporal_contract"
    assert references["SpatialTemporalContractNode"].main_utility_methods == ("process_operation",)
    assert references["SpatialTemporalContractNode"].subclass_implementation_methods
    assert references["SpatialTemporalContractNode"].step_output_schema_methods == ("process_operation",)
    assert references["SpatialTemporalContractNode"].planner_hooks == references["SpatialTemporalContractNode"].subclass_implementation_methods
    assert spatial_hook_signatures
    assert spatial_guidance_signatures
    assert any(
        keyword in signature
        for signature in spatial_guidance_signatures
        for keyword in ("guidance", "prompt")
    )

    assert render_workflow_method_signatures(
        references["WorkflowStepNode"].base_class,
        references["WorkflowStepNode"].subclass_implementation_methods,
    ) == ("process_input(user_input, dependency_results, session_state)",)
    assert render_workflow_method_signatures(
        references["WorkflowOperationNode"].base_class,
        references["WorkflowOperationNode"].subclass_implementation_methods,
    ) == ("process_operation(dependency_results, session_state)",)
    assert render_workflow_method_signatures(
        references["WorkflowFileNode"].base_class,
        references["WorkflowFileNode"].main_utility_methods,
    ) == ("save_files(files, session_state, storage_override)",)
    assert render_workflow_method_signatures(
        references["SpatialTemporalContractNode"].base_class,
        references["SpatialTemporalContractNode"].subclass_implementation_methods,
    ) == spatial_hook_signatures


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
    assert references["SpatialTemporalContractNode"].subclass_implementation_methods == workflow_node_reference_module._marked_method_names_from_ast(
        references["SpatialTemporalContractNode"].base_class,
        "node_subclass_implementation",
    )

    workflow_node_reference_module.workflow_node_references.cache_clear()


def test_workflow_step_meta_catalog_uses_meta_node_kind_metadata() -> None:
    catalog = render_workflow_step_meta_catalog()
    references = {reference.meta_node_kind: reference for reference in workflow_node_references()}
    spatial_hooks = ", ".join(references["SpatialTemporalContractNode"].subclass_implementation_methods)
    spatial_guidance_hooks = ", ".join(
        render_subclass_guidance_method_signatures(
            references["SpatialTemporalContractNode"].base_class,
            references["SpatialTemporalContractNode"].subclass_implementation_methods,
        )
    )

    assert "metaNodeKind: WorkflowStepNode" in catalog
    assert "capability category: input" in catalog
    assert "decorator-marked main utility methods: process_input" in catalog
    assert "StepRunOutput card/derived contract methods: build_step_output" in catalog
    assert f"decorator-marked subclass implementation hooks: {spatial_hooks}" in catalog
    assert f"parsed prompt/guidance helper methods from subclass-hook call path: {spatial_guidance_hooks}" in catalog
    assert "nodeKind:" not in catalog