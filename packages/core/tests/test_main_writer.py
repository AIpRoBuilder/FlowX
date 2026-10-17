# Modified 2026-10-13: generated entrypoint assertions use native JSON/configured pools.
# Modified 2026-10-14: assert the NODES tuple and reusable STEP_ID class map.
# Modified 2026-10-14: verify eager service replacement retains builder-owned session/artifact state.
# Modified 2026-10-14: replace graph-retry expectations with single-pass facade delegation.
# Modified 2026-10-14: isolate unused eager dependencies and pass builder state in graph requests.
# Modified 2026-10-16: patch and inspect the workflows the builder now owns directly.
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.builder_support import WorkflowArtifactState
from flowx_core.llm_client.coder import compose_session_marking_prompt
from flowx_core.tools.workflow_node_reference import (
    render_workflow_method_signatures,
    resolve_workflow_node_reference,
)
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_writer import (
    WorkflowFileNodeCoder,
    WorkflowStepNodeCoder,
)
from flowx_core.workflows.graph_generation import GraphGenerationRequest


class _FakeComponent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeCompletions:
    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="print('ok')"))]
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


@pytest.fixture
def _fake_builder_components(monkeypatch):
    """Keep placeholder providers out of real LLM component constructors."""
    for name in (
        "RequirementDisector", "GraphPlanner", "NodePlanElement", "PromptMainFileCoder",
        "RequestNodeFileCoder", "GenerateNodeTestFile", "AmendNodeFromTestLog",
    ):
        monkeypatch.setattr(agent_builder_module, name, _FakeComponent)


@pytest.fixture
def _fake_builder_workflows(monkeypatch, _fake_builder_components):
    """Factory/configuration tests do not need eagerly initialized native pools."""
    for name in (
        "GraphGenerationWorkflow", "NodeGenerationWorkflow",
        "NodeRuntimeModifierPipeline", "MainEntrypointGenerationWorkflow",
    ):
        monkeypatch.setattr(agent_builder_module, name, Mock(name=name))


def test_main_writer_always_renders_step_execution_route(tmp_path):
    writer = PromptMainFileCoder(client=_FakeClient())
    package_dir = tmp_path / "example_agent_output"
    package_dir.mkdir()
    graph_path = tmp_path / "graph_plan.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": name} for name in ("CollectInput", "ProcessInput")]}))
    for name in ("CollectInput", "ProcessInput"):
        (package_dir / f"{name}.py").write_text(f"class {name}: pass\n")

    output = writer.write_main_entrypoint(
        project_root_path=str(tmp_path),
        graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"),
        fastapi_host="0.0.0.0",
        fastapi_port=8000,
    ).read_text(encoding="utf-8")

    assert '@app.post("/api/run-step")' in output
    assert "WorkflowPipelinePool(" in output
    assert "pipeline_json=_build_pipeline_json()," in output
    assert 'size=workflow_config["pool_size"],' in output
    assert 'workflow_config.setdefault("pool_size", 4)' in output
    assert "NODES = (\n    CollectInput,\n    ProcessInput,\n)" in output
    assert output.count("{node.STEP_ID: node for node in NODES}") == 1
    assert "NODE_CLASSES = {node.STEP_ID: node for node in NODES}" in output
    assert "for node in NODES:" in output
    assert "node_classes=NODE_CLASSES," in output
    assert "register_class" not in output
    assert "nodes=_ordered_node_classes()" not in output
    assert "FLOWX_WORKFLOW_POOL_SIZE" not in output
    assert "engine.events(request_id, {step_id: normalized_input})" in output
    assert "lifespan=lifespan" in output
    assert "_run_step_events" not in output
    assert "pipeline_json_path=" not in output
    assert "thread_id=" not in output
    compile(output, "main.py", "exec")


def test_build_user_prompt_requires_uvicorn_launcher(tmp_path):
    writer = PromptMainFileCoder(client=_FakeClient())
    package_dir = tmp_path / "example_agent_output"
    package_dir.mkdir()
    graph_path = tmp_path / "graph_plan.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "CollectInput"}]}))
    (package_dir / "CollectInput.py").write_text("class CollectInput: pass\n")

    output = writer.write_main_entrypoint(
        project_root_path=str(tmp_path),
        graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"),
        fastapi_host="127.0.0.1",
        fastapi_port=9000,
        uvicorn_reload=True,
    ).read_text(encoding="utf-8")

    assert "uvicorn" in output
    assert "if __name__ == \"__main__\":" in output
    assert "uvicorn.run(app" in output
    assert "NODES = (\n    CollectInput,\n)" in output
    compile(output, "main.py", "exec")


@pytest.mark.usefixtures("_fake_builder_components")
def test_agent_builder_reset_llm_config_recreates_llm_components(monkeypatch, tmp_path):
    workflow_constructors = {}
    for attribute, workflow_name in (
        ("_graph_workflow", "GraphGenerationWorkflow"),
        ("_node_workflow", "NodeGenerationWorkflow"),
        ("_modifier_pipeline", "NodeRuntimeModifierPipeline"),
        ("_main_workflow", "MainEntrypointGenerationWorkflow"),
    ):
        constructor = Mock(name=workflow_name, side_effect=[Mock(), Mock()])
        monkeypatch.setattr(agent_builder_module, workflow_name, constructor)
        workflow_constructors[attribute] = constructor

    builder = AgentBuilder(
        api_key="old-key",
        model="old-model",
        provider="old-provider",
        root_dir=str(tmp_path),
        session_marking_prompt="Keep request_id on all file/text IO.",
    )

    expected_session_prompt = compose_session_marking_prompt(
        "Keep request_id on all file/text IO."
    )

    def workflow_dependencies():
        return {
            "_graph_workflow": {
                "analyzer": builder.analyzer, "planner": builder.planner,
                "graph_auditor": builder.graph_auditor,
            },
            "_node_workflow": {
                "node_planner": builder.node_planner, "node_coder": builder.node_coder,
                "node_auditor": builder.node_auditor,
            },
            "_modifier_pipeline": {
                "node_test_writer": builder.node_test_writer, "node_writer": builder.node_modifier_writer,
                "run_subprocess": builder._run_subprocess,
                "timeout_expired": builder._subprocess_timeout_expired(),
            },
            "_main_workflow": {
                "main_writer": builder.main_writer, "main_entry_auditor": builder.main_entry_auditor,
            },
        }

    initial_components = {
        name: getattr(builder, name) for name in (
            "analyzer", "planner", "node_planner", "main_writer",
            "node_coder", "node_test_writer", "node_modifier_writer",
        )
    }
    initial_workflows = {name: getattr(builder, name) for name in workflow_constructors}
    initial_runtime_service = builder._runtime_service
    initial_session = builder._session
    initial_artifact_state = builder.artifact_state
    published_artifacts = builder.artifact_state.workflow_artifacts
    graph_path = str(tmp_path / "workflow.json")
    node_path = str(tmp_path / "CollectInput.py")
    node_doc_path = str(tmp_path / "node_docs" / "CollectInput.md")
    published_artifact = WorkflowArtifactState(
        graph_plan_path=graph_path,
        node_location_map={"CollectInput": node_path},
        node_doc_paths={"CollectInput": node_doc_path},
    )
    published_artifacts["default"] = published_artifact
    builder.graph_plan_path = graph_path

    for name, dependencies in workflow_dependencies().items():
        workflow = initial_workflows[name]
        workflow_constructors[name].assert_called_once_with(**dependencies)
        workflow.run.assert_not_called()
        workflow.close.assert_not_called()

    builder.reset_llm_config(api_key="new-key", model="new-model", provider="new-provider")

    assert builder.api_key == "new-key"
    assert builder.model == "new-model"
    assert builder.provider == "new-provider"
    expected_settings = {
        "api_key": "new-key",
        "model": "new-model",
        "provider": "new-provider",
        "session_marking_prompt": expected_session_prompt,
    }
    for name, initial_component in initial_components.items():
        component = getattr(builder, name)
        assert component is not initial_component
        assert component.kwargs == expected_settings

    for name, dependencies in workflow_dependencies().items():
        workflow = getattr(builder, name)
        old_workflow = initial_workflows[name]
        assert workflow is not old_workflow
        old_workflow.close.assert_called_once_with()
        old_workflow.run.assert_not_called()
        assert workflow_constructors[name].call_count == 2
        workflow_constructors[name].assert_called_with(**dependencies)
        workflow.run.assert_not_called()
        workflow.close.assert_not_called()

    assert builder._runtime_service is not initial_runtime_service
    assert builder._session is initial_session
    assert builder.artifact_state is initial_artifact_state
    assert builder.artifact_state.workflow_artifacts is published_artifacts
    assert published_artifacts == {"default": published_artifact}
    assert published_artifacts["default"] is published_artifact
    assert published_artifact.graph_plan_path == graph_path
    assert published_artifact.node_location_map == {"CollectInput": node_path}
    assert published_artifact.node_doc_paths == {"CollectInput": node_doc_path}
    assert builder.graph_plan_path == graph_path


def test_node_planner_system_prompt_includes_session_marking_prompt():
    planner = NodePlanElement(
        client=_FakeClient(),
        session_marking_prompt=compose_session_marking_prompt(
            "Keep node brief examples request scoped."
        ),
    )

    assert "Request-scoped session marking policy" in planner.system_prompt
    assert "Keep node brief examples request scoped." in planner.system_prompt


def test_node_writer_contract_text_uses_reference_hook_signatures() -> None:
    step_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowStepNode")
    file_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowFileNode")

    step_hook = render_workflow_method_signatures(
        step_reference.base_class,
        step_reference.subclass_implementation_methods,
    )[0]
    file_main_utility = render_workflow_method_signatures(
        file_reference.base_class,
        file_reference.main_utility_methods,
    )[0]

    assert step_hook in WorkflowStepNodeCoder(client=_FakeClient()).get_node_contract_text()
    file_contract_text = WorkflowFileNodeCoder(client=_FakeClient()).get_node_contract_text()
    assert file_main_utility in file_contract_text
    assert "save_files_remote(files, session_state)" not in file_contract_text


@pytest.mark.usefixtures("_fake_builder_workflows")
def test_plan_graph_delegates_failure_once_without_repair(monkeypatch, tmp_path):
    analyzer = Mock(STEP_ID="analyze_requirement")
    planner = Mock(STEP_ID="plan_graph")
    graph_auditor = Mock(STEP_ID="audit_graph")
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", Mock(return_value=analyzer))
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", Mock(return_value=planner))
    monkeypatch.setattr(agent_builder_module, "GraphJsonAuditor", Mock(return_value=graph_auditor))
    graph_constructor = Mock()
    monkeypatch.setattr(agent_builder_module, "Graph", graph_constructor)
    feedback = "graph plan audit did not pass. Feedback:\nLine 3: invalid_graph - graph invalid"
    workflow_constructor = Mock()
    workflow = workflow_constructor.return_value
    workflow.run.side_effect = RuntimeError(feedback)
    # This is a facade test: arbitrary component mocks must not enter native pools.
    monkeypatch.setattr(agent_builder_module, "GraphGenerationWorkflow", workflow_constructor)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )
    workflow_constructor.assert_called_once_with(
        analyzer=analyzer, planner=planner, graph_auditor=graph_auditor,
    )
    assert builder._graph_workflow is workflow
    workflow.run.assert_not_called()
    requirement_path = str(tmp_path / "requirement.md")
    graph_path = str(tmp_path / "custom.json")

    with pytest.raises(RuntimeError) as error:
        builder.plan_graph(
            requirement_md_path=requirement_path, graph_plan_filename="custom.json", temperature=0.6,
        )

    assert str(error.value) == feedback
    workflow_constructor.assert_called_once()
    workflow.run.assert_called_once_with(GraphGenerationRequest(
        requirement_md_path=requirement_path, graph_plan_path=graph_path,
        graph_action="plan", temperature=0.6, skip_analyze_requirement=True,
        state={"build_session": builder._session},
    ))
    assert workflow.run.call_args.args[0].state["build_session"] is builder._session
    assert builder.requirement_md_path == requirement_path
    assert builder.graph_plan_path == graph_path
    assert builder.planned_graph is None
    planner.process_input.assert_not_called()
    planner.amend_file_with_feedback.assert_not_called()
    planner._write_mermaid_from_graph_json.assert_not_called()
    graph_auditor.audit_graph_json.assert_not_called()
    graph_constructor.assert_not_called()