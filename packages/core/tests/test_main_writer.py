from types import SimpleNamespace

import pytest

import flowx_core.agent_builder as agent_builder_module
from flowx_core.agent_builder import AgentBuilder
from flowx_core.architect.graph import NodeMeta
from flowx_core.architect.node_planner import NodePlanner
from flowx_core.llm_client.coder import compose_session_marking_prompt
from flowx_core.tools.workflow_node_reference import (
    render_workflow_method_signatures,
    resolve_workflow_node_reference,
)
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.worker.node_writer import (
    WorkflowFileNodeCoder,
    WorkflowOperationNodeCoder,
    WorkflowSkillNodeCoder,
    WorkflowStepNodeCoder,
)


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


class _FakePlanner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.plan_calls = []
        self.amend_calls = []

    def plan_from_file(self, requirement_md_path, graph_plan_path):
        self.plan_calls.append((requirement_md_path, graph_plan_path))

    def amend_file_with_feedback(self, graph_plan_path, amendment, temperature=0.35):
        self.amend_calls.append((graph_plan_path, amendment, temperature))


def test_build_user_prompt_includes_cron_trigger_requirements(tmp_path):
    writer = PromptMainFileCoder(client=_FakeClient())

    prompt = writer._build_user_prompt(
        project_root_path=tmp_path,
        graph_plan_json_path=tmp_path / "graph_plan.json",
        node_class_names=["CollectInput", "ProcessInput"],
        nodes_package_name="example_agent_output",
        fastapi_host="0.0.0.0",
        fastapi_port=8000,
        uvicorn_reload=False,
        requirement_analysis_result={
            "is_cron_task": True,
            "task_type": "cron",
            "crontab_expression": "0 9 * * *",
        },
    )

    assert '@app.post("/cron/start")' in prompt
    assert '@app.post("/api/run-step")' not in prompt
    assert "_run_all_steps_events" in prompt
    assert "asyncio.create_task" in prompt


def test_build_user_prompt_requires_uvicorn_launcher(tmp_path):
    writer = PromptMainFileCoder(client=_FakeClient())

    prompt = writer._build_user_prompt(
        project_root_path=tmp_path,
        graph_plan_json_path=tmp_path / "graph_plan.json",
        node_class_names=["CollectInput"],
        nodes_package_name="example_agent_output",
        fastapi_host="127.0.0.1",
        fastapi_port=9000,
        uvicorn_reload=True,
        requirement_analysis_result=None,
    )

    assert "uvicorn" in prompt
    assert "if __name__ == \"__main__\":" in prompt
    assert "uvicorn.run(app" in prompt


def test_agent_builder_reset_llm_config_recreates_llm_components(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)

    builder = AgentBuilder(
        api_key="old-key",
        model="old-model",
        provider="old-provider",
        root_dir=str(tmp_path),
        skills_root_path="skills-dir",
        session_marking_prompt="Keep request_id on all file/text IO.",
    )

    expected_session_prompt = compose_session_marking_prompt(
        "Keep request_id on all file/text IO."
    )

    initial_main_writer = builder.main_writer

    builder.reset_llm_config(api_key="new-key", model="new-model", provider="new-provider")

    assert builder.api_key == "new-key"
    assert builder.model == "new-model"
    assert builder.provider == "new-provider"
    assert builder.main_writer is not initial_main_writer
    assert builder.analyzer.kwargs == {
        "api_key": "new-key",
        "model": "new-model",
        "provider": "new-provider",
        "session_marking_prompt": expected_session_prompt,
    }
    assert builder.planner.kwargs == {
        "api_key": "new-key",
        "model": "new-model",
        "provider": "new-provider",
        "skills_root_path": "skills-dir",
        "session_marking_prompt": expected_session_prompt,
    }
    assert builder.node_planner.kwargs == {
        "api_key": "new-key",
        "model": "new-model",
        "provider": "new-provider",
        "skills_root_path": "skills-dir",
        "session_marking_prompt": expected_session_prompt,
    }
    assert builder.main_writer.kwargs == {
        "api_key": "new-key",
        "model": "new-model",
        "provider": "new-provider",
        "session_marking_prompt": expected_session_prompt,
    }


def test_agent_builder_make_node_coder_passes_session_marking_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "WorkflowStepNodeCoder", _FakeComponent)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
        session_marking_prompt="Use request_marker in generated node IO.",
    )

    coder = builder._make_node_coder(
        NodeMeta(
            name="CollectInput",
            type="",
            desc="Collect input",
            ext_data={"type": "user_input", "desc": "Collect request text"},
        )
    )

    assert coder.kwargs["session_marking_prompt"] == compose_session_marking_prompt(
        "Use request_marker in generated node IO."
    )


def test_agent_builder_make_node_coder_routes_operation_from_meta_node_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "WorkflowOperationNodeCoder", _FakeComponent)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )

    coder = builder._make_node_coder(
        NodeMeta(
            name="FetchRemoteData",
            type="",
            desc="Fetch from remote url",
            meta_node_kind="WorkflowOperationNode",
            ext_data={"type": "url", "desc": "remote api"},
        )
    )

    assert coder.kwargs["root_dir_path"] == str(tmp_path)


def test_node_planner_system_prompt_includes_session_marking_prompt():
    planner = NodePlanner(
        client=_FakeClient(),
        session_marking_prompt=compose_session_marking_prompt(
            "Keep node brief examples request scoped."
        ),
    )

    assert "Request-scoped session marking policy" in planner.system_prompt
    assert "Keep node brief examples request scoped." in planner.system_prompt


def test_node_writer_contract_text_uses_reference_hook_signatures() -> None:
    step_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowStepNode")
    operation_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowOperationNode")
    skill_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowSkillNode")
    file_reference = resolve_workflow_node_reference(meta_node_kind="WorkflowFileNode")

    step_hook = render_workflow_method_signatures(
        step_reference.base_class,
        step_reference.subclass_implementation_methods,
    )[0]
    operation_hook = render_workflow_method_signatures(
        operation_reference.base_class,
        operation_reference.subclass_implementation_methods,
    )[0]
    skill_hook = render_workflow_method_signatures(
        skill_reference.base_class,
        skill_reference.subclass_implementation_methods,
    )[0]
    file_main_utility = render_workflow_method_signatures(
        file_reference.base_class,
        file_reference.main_utility_methods,
    )[0]

    assert step_hook in WorkflowStepNodeCoder(client=_FakeClient()).get_node_contract_text()
    assert operation_hook in WorkflowOperationNodeCoder(client=_FakeClient()).get_node_contract_text()
    assert skill_hook in WorkflowSkillNodeCoder(client=_FakeClient()).get_node_contract_text()
    file_contract_text = WorkflowFileNodeCoder(client=_FakeClient()).get_node_contract_text()
    assert file_main_utility in file_contract_text
    assert "save_files_remote(files, session_state)" not in file_contract_text


def test_agent_builder_defaults_max_audit_rounds_to_seven(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
    )

    assert builder.max_audit_rounds == 7


def test_plan_graph_stops_after_configured_max_audit_rounds(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_builder_module, "RequirementDisector", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "GraphPlanner", _FakePlanner)
    monkeypatch.setattr(agent_builder_module, "NodePlanner", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "PromptMainFileCoder", _FakeComponent)
    monkeypatch.setattr(agent_builder_module, "Graph", lambda path: SimpleNamespace(path=path))

    builder = AgentBuilder(
        api_key="key",
        model="model",
        provider="provider",
        root_dir=str(tmp_path),
        max_audit_rounds=2,
    )
    builder.graph_auditor = SimpleNamespace(
        audit_graph_json=lambda _graph: (
            False,
            [SimpleNamespace(lineno=3, rule="invalid_graph", detail="graph invalid")],
        )
    )

    with pytest.raises(RuntimeError, match="graph plan audit did not pass after 2 attempt\\(s\\)"):
        builder.plan_graph(requirement_md_path=str(tmp_path / "requirement.md"))

    assert len(builder.planner.amend_calls) == 1