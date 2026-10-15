"""Offline coverage for the template-backed Coder subclass.

Modified 2026-10-13: explicit JSON class maps, binary file transport, and native wait gates.
Modified 2026-10-14: distinguish node tuples from STEP_ID maps and verify pool map identity.
Modified 2026-10-14: verify single-pass main writer -> auditor workflow requests.
Modified 2026-10-14: verify service delegation to the extracted main-entrypoint workflow.
Modified 2026-10-14: use explicit requests, eager pool reuse, and terminal cleanup.
Modified 2026-10-15: template-only requests omit temperature; public writer compatibility remains.
"""

import importlib.util
import base64
import json
import subprocess
import sys
from contextlib import closing
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from ag_ui_workflow import StepRunOutput
from fastapi.testclient import TestClient

from flowx_core import builder_services
from flowx_core.auditor.data import RuleViolation
from flowx_core.auditor.main_entrypoint_auditor import MainEntryPointAuditor
from flowx_core.llm_client.coder import Coder
from flowx_core.builder_services import MainEntrypointService
from flowx_core.worker.main_writer import PromptMainFileCoder
from flowx_core.workflows import main_entrypoint_generation
from flowx_core.workflows.main_entrypoint_generation import (
    MainEntrypointGenerationRequest,
    MainEntrypointGenerationWorkflow,
)


def _main_service(writer, auditor):
    return MainEntrypointService(
        main_writer=writer, main_entry_auditor=auditor,
    )


def test_main_writer_initializes_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    writer = PromptMainFileCoder(session_marking_prompt="Keep output request scoped.")

    assert isinstance(writer, Coder)
    assert writer.client is None
    assert writer.system_prompt == "Keep output request scoped."
    assert PromptMainFileCoder.write_code_to_file is Coder.write_code_to_file


def test_main_writer_renders_and_refreshes_template_without_llm(tmp_path) -> None:
    writer = PromptMainFileCoder()
    package_dir = tmp_path / "example_nodes"
    package_dir.mkdir()
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect", "enable": True}]}), encoding="utf-8")
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    main_path = package_dir / "main.py"

    result = writer.write_main_entrypoint(
        project_root_path=str(tmp_path),
        graph_plan_json_path=str(graph_path),
        output_path=str(main_path),
    )

    assert result == main_path
    assert "Collect.step_meta()" in main_path.read_text(encoding="utf-8")
    assert "from .Collect import Collect" in (package_dir / "__init__.py").read_text(encoding="utf-8")
    original_source = main_path.read_text(encoding="utf-8")
    assert writer.write_main_entrypoint(
        project_root_path=str(tmp_path), graph_plan_json_path=str(graph_path),
        output_path=str(main_path), temperature=0.9, max_tokens=1,
    ) == main_path
    assert main_path.read_text(encoding="utf-8") == original_source
    assert writer.client is None

    with pytest.raises(FileExistsError):
        writer.write_main_entrypoint(
            project_root_path=str(tmp_path),
            graph_plan_json_path=str(graph_path),
            output_path=str(main_path),
            overwrite=False,
        )

    (package_dir / "Publish.py").write_text("class Publish: pass\n", encoding="utf-8")
    graph_path.write_text(json.dumps({"nodes": [
        {"name": "Collect", "enable": True},
        {"name": "Publish", "enable": True},
    ]}), encoding="utf-8")

    assert writer.amend_code_with_feedback(str(main_path), "Refresh imports") == main_path
    assert "Publish.step_meta()" in main_path.read_text(encoding="utf-8")
    assert "from .Publish import Publish" in (package_dir / "__init__.py").read_text(encoding="utf-8")
    assert MainEntryPointAuditor().audit_file(str(main_path)) == (True, [])


def test_main_writer_renders_step_route_without_scheduling_code(tmp_path) -> None:
    writer = PromptMainFileCoder()
    package_dir = tmp_path / "nodes"
    package_dir.mkdir()
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")

    output = writer.write_main_entrypoint(
        project_root_path=str(tmp_path),
        graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"),
        fastapi_host="127.0.0.1",
        fastapi_port=9000,
        uvicorn_reload=True,
    ).read_text(encoding="utf-8")

    assert '@app.post("/api/run-step")' in output
    assert "uvicorn.run(app, host='127.0.0.1', port=9000, reload=True)" in output


def test_process_input_writes_then_amends_from_session_request(tmp_path, monkeypatch) -> None:
    writer = PromptMainFileCoder()
    write_main = Mock(wraps=writer.write_main_entrypoint)
    amend_main = Mock(wraps=writer.amend_code_with_feedback)
    monkeypatch.setattr(writer, "write_main_entrypoint", write_main)
    monkeypatch.setattr(writer, "amend_code_with_feedback", amend_main)
    package_dir = tmp_path / "nodes"
    package_dir.mkdir()
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    main_path = package_dir / "main.py"

    output = writer.process_input(
        "",
        {"graph": StepRunOutput(derived={"graph_plan_json_path": str(graph_path)})},
        {"PromptMainFileCoder::action": "write", "main_entrypoint": {
            "project_root_path": str(tmp_path), "output_path": str(main_path),
            "temperature": 0.9,
        }},
    )
    write_main.assert_called_once_with(
        project_root_path=str(tmp_path), graph_plan_json_path=str(graph_path),
        output_path=str(main_path), fastapi_host="0.0.0.0", fastapi_port=8000,
        uvicorn_reload=False, overwrite=True,
    )
    assert "temperature" not in write_main.call_args.kwargs
    amend_main.assert_not_called()
    assert output.derived["main_entrypoint_path"] == str(main_path)
    assert "Collect.step_meta()" in main_path.read_text(encoding="utf-8")

    (package_dir / "Publish.py").write_text("class Publish: pass\n", encoding="utf-8")
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}, {"name": "Publish"}]}), encoding="utf-8")
    result = writer.process_input("", {}, {"PromptMainFileCoder::action": "amend", "main_entrypoint": {
        "output_path": str(main_path), "amendment": "Refresh imports",
        "temperature": 0.4,
    }})
    amend_main.assert_called_once_with(
        str(main_path), "Refresh imports", language="python", overwrite=True, temperature=0.4,
    )
    write_main.assert_called_once()
    assert result.derived["main_entrypoint_path"] == str(main_path)
    assert "Publish.step_meta()" in main_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("session_state, message", [
    ({}, "session_state"),
    ({"main_entrypoint": {"action": "write"}}, "PromptMainFileCoder::action"),
    ({"PromptMainFileCoder::action": "plan", "main_entrypoint": {}}, "PromptMainFileCoder::action"),
    ({"PromptMainFileCoder::action": "write", "main_entrypoint": {"output_path": "x.py", "project_root_path": "."}}, "graph_plan_json_path"),
    ({"PromptMainFileCoder::action": "amend", "main_entrypoint": {"output_path": "x.py"}}, "amendment"),
])
def test_process_input_rejects_incomplete_requests(session_state, message) -> None:
    with pytest.raises(ValueError, match=message):
        PromptMainFileCoder().process_input("", {}, session_state)


@pytest.mark.parametrize("audit_ok", [True, False])
def test_builder_service_runs_main_writer_then_auditor_once(tmp_path, monkeypatch, audit_ok) -> None:
    package_dir = tmp_path / "nodes"
    package_dir.mkdir()
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")

    writer = PromptMainFileCoder(model="configured-main-writer")
    requests = []
    original_process_input = PromptMainFileCoder.process_input

    def capture_request(self, user_input, dependency_results, session_state):
        assert self.model == writer.model
        assert dependency_results == {}
        assert "temperature" not in session_state["main_entrypoint"]
        requests.append((session_state["PromptMainFileCoder::action"], dict(session_state["main_entrypoint"])))
        return original_process_input(self, user_input, dependency_results, session_state)

    monkeypatch.setattr(PromptMainFileCoder, "process_input", capture_request)
    amendment = Mock(side_effect=AssertionError("main generation must not amend or retry"))
    monkeypatch.setattr(PromptMainFileCoder, "amend_code_with_feedback", amendment)
    audit_calls = []
    original_audit_input = MainEntryPointAuditor.process_input

    def capture_audit(self, user_input, dependency_results, session_state):
        assert list(dependency_results) == [writer.STEP_ID]
        assert dependency_results[writer.STEP_ID].derived["main_entrypoint_path"] == str(package_dir / "main.py")
        return original_audit_input(self, user_input, dependency_results, session_state)

    def audit_file(self, file_path, nodes_root=None):
        assert Path(file_path).is_file(), "the writer must run before the auditor"
        audit_calls.append((file_path, nodes_root))
        return audit_ok, [] if audit_ok else [RuleViolation("(file)", "missing", "refresh", 1)]

    monkeypatch.setattr(MainEntryPointAuditor, "process_input", capture_audit)
    monkeypatch.setattr(MainEntryPointAuditor, "audit_file", audit_file)
    close_engine = Mock(wraps=main_entrypoint_generation.close_workflow_engine)
    init_engine = Mock(wraps=main_entrypoint_generation.init_workflow_engine)
    monkeypatch.setattr(main_entrypoint_generation, "close_workflow_engine", close_engine)
    monkeypatch.setattr(main_entrypoint_generation, "init_workflow_engine", init_engine)

    request = MainEntrypointGenerationRequest(
        project_root_path=str(package_dir), graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"), fastapi_host="127.0.0.1",
        fastapi_port=9001,
    )
    assert "temperature" not in {setting.name for setting in fields(request)}
    assert not hasattr(request, "temperature")

    with closing(_main_service(writer, MainEntryPointAuditor())) as service:
        engine = service.workflow._engine
        assert engine is not None
        init_engine.assert_called_once()
        assert init_engine.call_args.args[0] == {
            "main_generate": PromptMainFileCoder,
            "main_audit": MainEntryPointAuditor,
        }
        close_engine.assert_not_called()

        if audit_ok:
            assert service.run(request) == request.output_path
        else:
            with pytest.raises(RuntimeError, match="main entrypoint audit did not pass") as error:
                service.run(request)
            assert "Line 1: missing - refresh" in str(error.value)

        assert set(vars(service)) == {"main_writer", "main_entry_auditor", "workflow"}
        assert request.state == {}
        assert [action for action, _ in requests] == ["write"]
        assert requests[0][1] == {
            "project_root_path": request.project_root_path,
            "graph_plan_json_path": request.graph_plan_json_path,
            "output_path": request.output_path,
            "fastapi_host": request.fastapi_host,
            "fastapi_port": request.fastapi_port,
        }
        assert "temperature" not in requests[0][1]
        assert audit_calls == [(request.output_path, request.project_root_path)]
        source = Path(request.output_path).read_text(encoding="utf-8")
        assert "Collect.step_meta()" in source
        assert "uvicorn.run(app, host='127.0.0.1', port=9001, reload=False)" in source
        amendment.assert_not_called()
        init_engine.assert_called_once()
        assert service.workflow._engine is engine
        assert not engine.pool.closed
        with engine.pool.lease() as (_, param):
            assert param.state == {}
        close_engine.assert_not_called()

    close_engine.assert_called_once_with(engine)
    assert service.workflow._engine is None
    assert engine.pool.closed


def test_main_entrypoint_service_delegates_build_request_to_workflow(tmp_path, monkeypatch) -> None:
    workflow = Mock()
    workflow.run.return_value = str(tmp_path / "generated-main.py")
    constructor = Mock(return_value=workflow)
    monkeypatch.setattr(builder_services, "MainEntrypointGenerationWorkflow", constructor)
    writer, auditor = object(), object()
    state = {"marker": {"value": "caller-owned"}}
    request = MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=str(tmp_path / "requested-workflow.json"),
        output_path=str(tmp_path / "entrypoint.py"), fastapi_host="127.0.0.1",
        fastapi_port=9002, state=state,
    )

    with closing(_main_service(writer, auditor)) as service:
        constructor.assert_called_once_with(main_writer=writer, main_entry_auditor=auditor)
        assert service.workflow is workflow
        workflow.run.assert_not_called()
        workflow.close.assert_not_called()

        output = service.run(request)

        workflow.run.assert_called_once_with(request)
        assert workflow.run.call_args.args[0] is request
        assert workflow.run.call_args.kwargs == {}
        assert output == workflow.run.return_value
        assert set(vars(service)) == {"main_writer", "main_entry_auditor", "workflow"}
        assert request.state is state
        constructor.assert_called_once()
        workflow.close.assert_not_called()

    workflow.close.assert_called_once_with()


def test_builder_main_workflow_preserves_template_and_explicit_amendment_context(tmp_path) -> None:
    package_dir = tmp_path / "nodes"
    package_dir.mkdir()
    graph_path = package_dir / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    template_path = tmp_path / "custom.tmpl"
    default_writer = PromptMainFileCoder()
    template_path.write_text(default_writer._template_source + "\n# custom template\n", encoding="utf-8")
    writer = PromptMainFileCoder(template_path=str(template_path))
    request = MainEntrypointGenerationRequest(
        project_root_path=str(package_dir), graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"),
    )

    with closing(_main_service(writer, MainEntryPointAuditor())) as service:
        output = service.run(request)

        assert MainEntryPointAuditor().audit_file(output, nodes_root=str(package_dir)) == (True, [])
        assert "# custom template" in Path(output).read_text(encoding="utf-8")
        assert Path(output) in writer._render_context_by_target

    assert service.workflow._engine is None
    assert writer.amend_code_with_feedback(output, "Refresh the entrypoint") == Path(output)


def test_builder_main_workflow_retains_pool_when_writer_raises(tmp_path, monkeypatch) -> None:
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
    (tmp_path / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    writer = PromptMainFileCoder()
    original_write = PromptMainFileCoder.write_main_entrypoint
    original_audit_input = MainEntryPointAuditor.process_input
    monkeypatch.setattr(
        PromptMainFileCoder, "write_main_entrypoint",
        Mock(side_effect=ValueError("writer failed")),
    )
    audit_input = Mock(side_effect=AssertionError("audit must not run after writer failure"))
    monkeypatch.setattr(MainEntryPointAuditor, "process_input", audit_input)
    close_engine = Mock(wraps=main_entrypoint_generation.close_workflow_engine)
    init_engine = Mock(wraps=main_entrypoint_generation.init_workflow_engine)
    monkeypatch.setattr(main_entrypoint_generation, "close_workflow_engine", close_engine)
    monkeypatch.setattr(main_entrypoint_generation, "init_workflow_engine", init_engine)
    request = MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=str(graph_path),
        output_path=str(tmp_path / "main.py"), state={"failed_request": True},
    )

    with closing(_main_service(writer, MainEntryPointAuditor())) as service:
        engine = service.workflow._engine
        assert engine is not None
        init_engine.assert_called_once()
        with pytest.raises(RuntimeError, match="writer failed"):
            service.run(request)

        audit_input.assert_not_called()
        close_engine.assert_not_called()
        assert service.workflow._engine is engine
        assert not engine.pool.closed
        assert request.state == {"failed_request": True}
        with engine.pool.lease() as (_, param):
            assert param.state == {}

        monkeypatch.setattr(PromptMainFileCoder, "write_main_entrypoint", original_write)
        monkeypatch.setattr(MainEntryPointAuditor, "process_input", original_audit_input)
        recovery_request = MainEntrypointGenerationRequest(
            project_root_path=str(tmp_path), graph_plan_json_path=str(graph_path),
            output_path=str(tmp_path / "recovered.py"),
        )
        assert service.run(recovery_request) == recovery_request.output_path
        assert MainEntryPointAuditor().audit_file(recovery_request.output_path, nodes_root=str(tmp_path)) == (True, [])
        init_engine.assert_called_once()
        close_engine.assert_not_called()
        assert service.workflow._engine is engine
        assert not engine.pool.closed
        with engine.pool.lease() as (_, param):
            assert param.state == {}

    close_engine.assert_called_once_with(engine)
    assert service.workflow._engine is None
    assert engine.pool.closed


def test_main_entrypoint_workflow_runs_direct_build_request(tmp_path, monkeypatch) -> None:
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")
    (tmp_path / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    writer = PromptMainFileCoder()
    close_engine = Mock(wraps=main_entrypoint_generation.close_workflow_engine)
    init_engine = Mock(wraps=main_entrypoint_generation.init_workflow_engine)
    monkeypatch.setattr(main_entrypoint_generation, "close_workflow_engine", close_engine)
    monkeypatch.setattr(main_entrypoint_generation, "init_workflow_engine", init_engine)
    request = MainEntrypointGenerationRequest(
        project_root_path=str(tmp_path), graph_plan_json_path=str(graph_path),
        output_path=str(tmp_path / "main.py"),
    )

    with closing(MainEntrypointGenerationWorkflow(writer, MainEntryPointAuditor())) as workflow:
        engine = workflow._engine
        assert engine is not None
        init_engine.assert_called_once()
        close_engine.assert_not_called()
        output = workflow.run(request)

        assert output == request.output_path
        assert MainEntryPointAuditor().audit_file(output, nodes_root=str(tmp_path)) == (True, [])
        assert Path(output) in writer._render_context_by_target
        assert workflow._engine is engine
        assert not engine.pool.closed
        with engine.pool.lease() as (_, param):
            assert param.state == {}
        init_engine.assert_called_once()
        close_engine.assert_not_called()

    assert workflow._engine is None
    assert engine.pool.closed
    workflow.close()
    close_engine.assert_called_once_with(engine)
    with pytest.raises(RuntimeError, match="main entrypoint workflow is closed"):
        workflow.run(request)
    init_engine.assert_called_once()
    close_engine.assert_called_once_with(engine)


@pytest.mark.parametrize("runner_kind", ["service", "workflow"])
def test_main_entrypoint_requests_reuse_pool_with_independent_targets_and_state(tmp_path, monkeypatch, runner_kind) -> None:
    first_root, second_root = tmp_path / "first_nodes", tmp_path / "second_nodes"
    for root, node_name in ((first_root, "Collect"), (second_root, "Publish")):
        root.mkdir()
        (root / f"{node_name}.py").write_text(f"class {node_name}: pass\n", encoding="utf-8")
        (root / "workflow.json").write_text(json.dumps({"nodes": [{"name": node_name}]}), encoding="utf-8")
    marker = {"value": "caller-owned"}
    state = {"marker": marker}
    first_request = MainEntrypointGenerationRequest(
        project_root_path=str(first_root), graph_plan_json_path=str(first_root / "workflow.json"),
        output_path=str(first_root / "main.py"), fastapi_host="127.0.0.1", fastapi_port=9003,
        state=state,
    )
    second_request = MainEntrypointGenerationRequest(
        project_root_path=str(second_root), graph_plan_json_path=str(second_root / "workflow.json"),
        output_path=str(second_root / "main.py"), fastapi_port=9004,
    )
    native_params, observed_states = [], []
    original_process_input = PromptMainFileCoder.process_input

    def capture_request(self, user_input, dependency_results, session_state):
        param = self.get_workflow_param()
        assert session_state is param.state
        assert "previous_target" not in session_state
        assert "temperature" not in session_state["main_entrypoint"]
        # Lease cleanup replaces the state dict, so track the native parameter.
        native_params.append(param)
        observed_states.append(dict(session_state))
        session_state["previous_target"] = session_state["main_entrypoint"]["output_path"]
        return original_process_input(self, user_input, dependency_results, session_state)

    monkeypatch.setattr(PromptMainFileCoder, "process_input", capture_request)
    close_engine = Mock(wraps=main_entrypoint_generation.close_workflow_engine)
    init_engine = Mock(wraps=main_entrypoint_generation.init_workflow_engine)
    monkeypatch.setattr(main_entrypoint_generation, "close_workflow_engine", close_engine)
    monkeypatch.setattr(main_entrypoint_generation, "init_workflow_engine", init_engine)
    writer, auditor = PromptMainFileCoder(), MainEntryPointAuditor()

    with closing(
        _main_service(writer, auditor) if runner_kind == "service"
        else MainEntrypointGenerationWorkflow(writer, auditor)
    ) as runner:
        workflow = runner.workflow if runner_kind == "service" else runner
        engine = workflow._engine
        assert engine is not None
        init_engine.assert_called_once()
        sources = []
        for request in (first_request, first_request, second_request):
            assert runner.run(request) == request.output_path
            sources.append(Path(request.output_path).read_text(encoding="utf-8"))
            assert workflow._engine is engine
            assert not engine.pool.closed
            assert all(native_param.state == {} for native_param in native_params)
            with engine.pool.lease() as (_, param):
                assert param.state == {}
            init_engine.assert_called_once()
            close_engine.assert_not_called()

        assert len(observed_states) == 3
        assert observed_states[0]["marker"] is marker
        assert observed_states[1]["marker"] is marker
        assert "marker" not in observed_states[2]
        assert state == {"marker": {"value": "caller-owned"}}
        assert second_request.state == {}
        assert sources[0] == sources[1] == Path(first_request.output_path).read_text(encoding="utf-8")
        assert "Collect.step_meta()" in sources[0] and "Publish.step_meta()" not in sources[0]
        assert "Publish.step_meta()" in sources[2] and "Collect.step_meta()" not in sources[2]
        assert "uvicorn.run(app, host='127.0.0.1', port=9003, reload=False)" in sources[0]
        assert "uvicorn.run(app, host='0.0.0.0', port=9004, reload=False)" in sources[2]
        assert set(writer._render_context_by_target) == {
            Path(first_request.output_path), Path(second_request.output_path),
        }
        if runner_kind == "service":
            assert set(vars(runner)) == {"main_writer", "main_entry_auditor", "workflow"}

    close_engine.assert_called_once_with(engine)
    assert workflow._engine is None
    assert engine.pool.closed


def _render_backend(tmp_path, sources=None, *, workflow_config=None):
    package_dir = tmp_path / f"generated_nodes_{uuid4().hex}"
    package_dir.mkdir()
    if sources is None:
        sources = {
            name: f'''class {name}:
    STEP_ID = {step_id!r}
    DEPENDENCIES = []

    @classmethod
    def step_meta(cls):
        return {{"id": cls.STEP_ID, "metaNodeKind": {ext_type!r}}}
'''
            for name, step_id, ext_type in (
            ("Collect", "collect", "WorkflowStepNode"),
            ("Upload", "upload", "WorkflowFileNode"),
            )
        }
    for name, source in sources.items():
        (package_dir / f"{name}.py").write_text(source, encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    # FlowX planning JSON is deliberately not a native pydaograph pipeline document.
    planning_config = {"nodes": [
        {"name": name, "type": "", "meta_type": "GCondition", "loop": 99,
         "depends": ["planning-only-dependency"]}
        for name in sources
    ] + [{"name": "Disabled", "enable": False}]}
    if workflow_config is not None:
        planning_config.update(workflow_config)
    graph_path.write_text(json.dumps(planning_config), encoding="utf-8")
    return PromptMainFileCoder().write_main_entrypoint(
        project_root_path=str(tmp_path),
        graph_plan_json_path=str(graph_path),
        output_path=str(package_dir / "main.py"),
    )


@pytest.fixture
def generated_backend(tmp_path, monkeypatch):
    path = _render_backend(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = importlib.util.spec_from_file_location(f"entrypoint_{uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        for name in list(sys.modules):
            if name == path.parent.name or name.startswith(path.parent.name + "."):
                sys.modules.pop(name, None)


def _install_strict_runtime(module, monkeypatch):
    calls = []
    pools, engines = [], []

    class Pool:
        def __init__(self, *, pipeline_json, node_classes, size):
            assert isinstance(pipeline_json, str)
            assert node_classes is module.NODE_CLASSES
            assert isinstance(node_classes, dict)
            self.pipeline_json, self.size = pipeline_json, size
            self.node_classes = node_classes
            self.graph = json.loads(pipeline_json)
            assert set(self.graph) == {"nodes"}
            assert set(node_classes) == {node["name"] for node in self.graph["nodes"]}
            for node in self.graph["nodes"]:
                assert set(node) == {"name", "type", "depends", "meta_type", "loop"}
                assert node_classes[node["name"]].__name__ == node["type"]
                assert node["meta_type"] == "GNode" and node["loop"] == 1
            self.closed = False
            pools.append(self)

        def close(self):
            calls.append(("pool.close",))
            self.closed = True

    class Engine:
        def __init__(self, *, graph_config_id, steps_meta, pool):
            self.graph_config_id, self.steps_meta, self.pool = graph_config_id, steps_meta, pool
            engines.append(self)

        def events(self, request_id, inputs):
            calls.append(("events", request_id, inputs))
            yield 'data: {"type":"RUN_FINISHED"}\n\n'

        def close(self, request_id=None):
            calls.append(("engine.close", request_id))

    monkeypatch.setattr(module, "WorkflowPipelinePool", Pool)
    monkeypatch.setattr(module, "WorkflowEngine", Engine)
    return SimpleNamespace(calls=calls, pools=pools, engines=engines)


def test_generated_routes_preserve_inputs_and_rotate_request_ids(generated_backend, monkeypatch, tmp_path):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    workflow_config = json.loads(module.PIPELINE_JSON_PATH.read_text(encoding="utf-8"))
    workflow_config["pool_size"] = 2
    module.PIPELINE_JSON_PATH.write_text(json.dumps(workflow_config), encoding="utf-8")
    monkeypatch.setenv("FLOWX_WORKFLOW_POOL_SIZE", "not-an-integer")
    with TestClient(module.app) as client:
        assert client.get("/").json()["ok"] is True
        first = client.post("/api/run-step", json={"sessionId": "a", "stepId": "collect", "input": {"value": "Ada"}})
        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["cache-control"] == "no-cache"
        assert first.headers["connection"] == "keep-alive"
        request_a = module.SESSION_REQUESTS["a"]
        assert runtime.calls[-1] == ("events", request_a, {"collect": {"value": "Ada"}})

        upload_path = tmp_path / "input.txt"
        upload_path.write_bytes(b"uploaded content")
        client.post("/api/run-step", json={"sessionId": "a", "stepId": "upload", "file_path": str(upload_path), "input": "ignored"})
        assert runtime.calls[-1] == ("events", request_a, {"upload": b"uploaded content"})
        client.post("/api/run-step", json={"sessionId": "b", "stepId": "upload", "input": {"base64": ""}})
        request_b = module.SESSION_REQUESTS["b"]
        assert request_b != request_a
        assert runtime.calls[-1] == ("events", request_b, {"upload": b""})
        client.post("/api/run-step", json={"sessionId": "b", "stepId": "collect"})
        assert runtime.calls[-1] == ("events", request_b, {"collect": None})

        reset = client.post("/api/reset-session", json={"sessionId": "a"}).json()
        assert reset == {"ok": True, "sessionId": "a", "threadId": module.GRAPH_CONFIG_ID, "runId": module.SESSION_REQUESTS["a"]}
        assert reset["runId"] != request_a
        assert runtime.calls[-1] == ("engine.close", request_a)
        assert module.SESSION_REQUESTS["b"] == request_b
        client.post("/api/run-step", json={"sessionId": "a", "stepId": "collect", "input": "new"})
        assert runtime.calls[-1] == ("events", reset["runId"], {"collect": "new"})

        before = list(runtime.calls)
        unknown_reset = client.post("/api/reset-session", json={"sessionId": "unseen"})
        assert unknown_reset.status_code == 200
        assert runtime.calls == before  # No engine.close(None), which would cancel everyone.
        bad_step = client.post("/api/run-step", json={"sessionId": "invalid", "stepId": "missing"})
        assert bad_step.status_code == 404
        assert "invalid" not in module.SESSION_REQUESTS
        assert runtime.calls == before
        assert len(runtime.pools) == len(runtime.engines) == 1
        assert runtime.pools[0].size == 2
        assert runtime.pools[0].graph == {"nodes": [
            {"name": "collect", "type": "Collect", "depends": [], "meta_type": "GNode", "loop": 1},
            {"name": "upload", "type": "Upload", "depends": [], "meta_type": "GNode", "loop": 1},
        ]}
        assert runtime.pools[0].node_classes == {"collect": module.Collect, "upload": module.Upload}
        assert not runtime.pools[0].closed

    assert runtime.calls[-2:] == [("engine.close", None), ("pool.close",)]
    assert runtime.pools[0].closed
    assert module.ENGINE is None
    assert module.SESSION_REQUESTS == {}


@pytest.mark.parametrize("config,expected_size", [({}, 4), ({"pool_size": 1}, 1), ({"pool_size": 3}, 3)])
def test_generated_pool_reads_config_once_at_startup(generated_backend, monkeypatch, config, expected_size):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    module.PIPELINE_JSON_PATH.write_text(json.dumps(config), encoding="utf-8")
    read_text = type(module.PIPELINE_JSON_PATH).read_text
    reads = []

    def tracked_read(path, *args, **kwargs):
        if path == module.PIPELINE_JSON_PATH:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(module.PIPELINE_JSON_PATH), "read_text", tracked_read)
    monkeypatch.setenv("FLOWX_WORKFLOW_POOL_SIZE", "100")
    with TestClient(module.app) as client:
        assert reads == [module.PIPELINE_JSON_PATH]
        assert runtime.pools[0].size == expected_size
        # Routes and resets use the startup snapshot, even if the file changes.
        module.PIPELINE_JSON_PATH.write_text('{"pool_size": false}', encoding="utf-8")
        assert client.post("/api/run-step", json={"sessionId": "a", "stepId": "collect"}).status_code == 200
        assert client.post("/api/reset-session", json={"sessionId": "a"}).status_code == 200
        assert reads == [module.PIPELINE_JSON_PATH]
        assert len(runtime.pools) == 1 and runtime.pools[0].size == expected_size
    assert reads == [module.PIPELINE_JSON_PATH]


@pytest.mark.parametrize("pool_size", [True, False, 0, -1, 1.5, 2.0, "2", "", None, [], {}])
def test_generated_pool_rejects_invalid_config_size(generated_backend, monkeypatch, pool_size):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    module.PIPELINE_JSON_PATH.write_text(json.dumps({"pool_size": pool_size}), encoding="utf-8")
    with pytest.raises(ValueError, match="pool_size must be a positive integer"):
        with TestClient(module.app):
            pass
    assert runtime.pools == runtime.engines == []
    assert module.PIPELINE_POOL is None


@pytest.mark.parametrize("config", [[], None, "planning", 4])
def test_generated_pool_requires_config_object(generated_backend, monkeypatch, config):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    module.PIPELINE_JSON_PATH.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="config must be a JSON object"):
        with TestClient(module.app):
            pass
    assert runtime.pools == runtime.engines == []


def test_generated_json_pool_uses_step_id_class_map_without_global_registration(generated_backend, monkeypatch):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    assert isinstance(module.NODES, tuple)
    assert module.NODES == (module.Collect, module.Upload)
    assert module.NODE_CLASSES == {node.STEP_ID: node for node in module.NODES}
    with TestClient(module.app):
        assert runtime.pools[0].node_classes is module.NODE_CLASSES
        assert runtime.pools[0].node_classes == {"collect": module.Collect, "upload": module.Upload}
        assert runtime.pools[0].graph["nodes"][0]["type"] == "Collect"
        assert not hasattr(module, "register_class")


@pytest.mark.parametrize("payload, expected", [
    ({"base64": "AAH/"}, b"\x00\x01\xff"),
    ({"content_base64": ""}, b""),
    ({"data": "data:application/octet-stream;base64,AAH/"}, b"\x00\x01\xff"),
    ({"files": [{"base64": "AAH/"}]}, b"\x00\x01\xff"),
    ({"uploads": [{"base64": "AAH/"}]}, b"\x00\x01\xff"),
    (None, None),
])
def test_generated_file_route_decodes_one_upload(generated_backend, monkeypatch, payload, expected):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    with TestClient(module.app) as client:
        response = client.post("/api/run-step", json={"sessionId": "file", "stepId": "upload", "input": payload})
        assert response.status_code == 200
        assert runtime.calls[-1] == ("events", module.SESSION_REQUESTS["file"], {"upload": expected})


@pytest.mark.parametrize("payload", [
    {"files": []}, {"files": [None]}, {"files": [{"base64": ""}, {"base64": ""}]},
    {"uploads": "not-a-list"}, {"base64": "!not base64"},
    {"data": "data:text/plain,not-base64"}, {"path": ""}, {"path": 1}, {},
])
def test_generated_file_route_rejects_bad_upload_before_starting_run(generated_backend, monkeypatch, payload):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    with TestClient(module.app) as client:
        response = client.post("/api/run-step", json={"sessionId": "file", "stepId": "upload", "input": payload})
        assert response.status_code == 422
        assert runtime.calls == []
        assert "file" not in module.SESSION_REQUESTS


def test_generated_file_route_reports_missing_path(generated_backend, monkeypatch, tmp_path):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    with TestClient(module.app) as client:
        response = client.post("/api/run-step", json={
            "sessionId": "file", "stepId": "upload", "file_path": str(tmp_path / "missing"),
        })
        assert response.status_code == 422
        assert runtime.calls == []
        assert "file" not in module.SESSION_REQUESTS


def test_generated_backend_requires_lifespan_and_never_rebuilds_pool(generated_backend, monkeypatch):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    client = TestClient(module.app)
    assert client.post("/api/run-step", json={"sessionId": "a", "stepId": "collect"}).status_code == 503
    assert client.post("/api/reset-session", json={"sessionId": "a"}).status_code == 503
    assert runtime.pools == runtime.engines == []
    with client:
        assert module.ENGINE is runtime.engines[0]
    assert client.post("/api/run-step", json={"sessionId": "a", "stepId": "collect"}).status_code == 503
    with pytest.raises(RuntimeError, match="once per process"):
        with TestClient(module.app):
            pass
    assert len(runtime.pools) == len(runtime.engines) == 1


def test_generated_lifespan_closes_pool_when_engine_startup_fails(generated_backend, monkeypatch):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)

    def fail_engine(**kwargs):
        raise ValueError("engine startup failed")

    monkeypatch.setattr(module, "WorkflowEngine", fail_engine)
    with pytest.raises(ValueError, match="engine startup failed"):
        with TestClient(module.app):
            pass
    assert runtime.calls == [("pool.close",)]
    assert runtime.pools[0].closed


def test_generated_lifespan_closes_pool_even_if_engine_close_fails(generated_backend, monkeypatch):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    with pytest.raises(RuntimeError, match="engine close failed"):
        with TestClient(module.app):
            def fail_close():
                runtime.calls.append(("engine.close.failed",))
                raise RuntimeError("engine close failed")
            runtime.engines[0].close = fail_close
    assert runtime.calls == [("engine.close.failed",), ("pool.close",)]


@pytest.mark.parametrize("nodes,expected", [
    ([SimpleNamespace(STEP_ID="b", DEPENDENCIES=["a"]), SimpleNamespace(STEP_ID="a", DEPENDENCIES=[])], ["a", "b"]),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES=["missing"])], None),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES=["b"]), SimpleNamespace(STEP_ID="b", DEPENDENCIES=["a"])], None),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES=[]), SimpleNamespace(STEP_ID="a", DEPENDENCIES=[])], None),
    ([SimpleNamespace(STEP_ID="", DEPENDENCIES=[])], None),
    ([SimpleNamespace(STEP_ID=" ", DEPENDENCIES=[])], None),
    ([SimpleNamespace(STEP_ID=None, DEPENDENCIES=[])], None),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES=["a"])], None),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES="a")], None),
    ([SimpleNamespace(STEP_ID="a", DEPENDENCIES=[None])], None),
])
def test_generated_pool_node_ordering(generated_backend, monkeypatch, nodes, expected):
    monkeypatch.setattr(generated_backend, "NODES", tuple(nodes))
    if expected is None:
        with pytest.raises(ValueError):
            generated_backend._ordered_node_classes()
    else:
        assert [node.STEP_ID for node in generated_backend._ordered_node_classes()] == expected


@pytest.mark.parametrize("dependencies", [["missing"], ["upload"]])
def test_generated_invalid_dag_fails_before_registration_or_pool(generated_backend, monkeypatch, dependencies):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    monkeypatch.setattr(module.Collect, "DEPENDENCIES", dependencies)
    monkeypatch.setattr(module.Upload, "DEPENDENCIES", ["collect"])
    with pytest.raises(ValueError, match="cycle|unknown STEP_ID"):
        with TestClient(module.app):
            pass
    assert runtime.pools == runtime.engines == []


def test_generated_native_json_uses_class_step_ids_and_dependencies(generated_backend, monkeypatch):
    module = generated_backend
    runtime = _install_strict_runtime(module, monkeypatch)
    monkeypatch.setattr(module.Upload, "DEPENDENCIES", ["collect"])
    monkeypatch.setattr(module, "NODES", (module.Upload, module.Collect))
    monkeypatch.setattr(module, "NODE_CLASSES", {node.STEP_ID: node for node in module.NODES})
    with TestClient(module.app):
        assert runtime.pools[0].graph == {"nodes": [
            {"name": "collect", "type": "Collect", "depends": [], "meta_type": "GNode", "loop": 1},
            {"name": "upload", "type": "Upload", "depends": ["collect"], "meta_type": "GNode", "loop": 1},
        ]}
        assert runtime.pools[0].node_classes == {"collect": module.Collect, "upload": module.Upload}


def _gated_node_source(name, step_id, dependency):
    return f'''from ag_ui_workflow import WorkflowStepNode, StepRunOutput

class {name}(WorkflowStepNode):
    STEP_ID = {step_id!r}
    DEPENDENCIES = [{dependency!r}]
    executed = []

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            value = param.inputs.get(self.STEP_ID)
            ready = isinstance(value, str) and bool(value.strip())
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, user_input, dependency_results, session_state):
        self.executed.append((self.get_workflow_param().request_id, user_input))
        return StepRunOutput(derived={{"value": user_input}})
'''


def test_generated_template_executes_real_nodes_and_reuses_native_pool(tmp_path):
    sources = {
        "Finish": '''from ag_ui_workflow import WorkflowStepNode, StepRunOutput
class Finish(WorkflowStepNode):
    STEP_ID = "finish"
    DEPENDENCIES = ["confirm"]
    INPUT_REQUIRED = False
    def process_input(self, user_input, dependency_results, session_state):
        return StepRunOutput(derived={"confirmed": dependency_results["confirm"].derived["value"]})
''',
        "Confirm": _gated_node_source("Confirm", "confirm", "ask"),
        "Ask": _gated_node_source("Ask", "ask", "prepare"),
        "Prepare": '''from ag_ui_workflow import WorkflowStepNode, StepRunOutput
class Prepare(WorkflowStepNode):
    STEP_ID = "prepare"
    DEPENDENCIES = []
    INPUT_REQUIRED = True  # Metadata does not install a wait callback.
    executed = []
    def process_input(self, user_input, dependency_results, session_state):
        self.executed.append(self.get_workflow_param().request_id)
        return StepRunOutput(derived={"ready": True})
''',
    }
    path = _render_backend(tmp_path, sources, workflow_config={"pool_size": 2})
    assert MainEntryPointAuditor().audit_file(str(path)) == (True, [])
    # Native bindings do not support pool recreation; isolate this process-lifetime test.
    script = f'''import importlib.util
import json
import os
import sys
from fastapi.testclient import TestClient
from pydaograph import GElement
sys.path.insert(0, {str(tmp_path)!r})
spec = importlib.util.spec_from_file_location("generated_main", {str(path)!r})
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
spec.loader.exec_module(main)
assert all(node.__name__ not in GElement.registeredClasses() for node in main.NODES)
real_pool = main.WorkflowPipelinePool
pool_sources = []
def checked_pool(*, pipeline_json, node_classes, size):
    pool_sources.append((json.loads(pipeline_json), size))
    assert size == 2
    assert pool_sources[-1][0] == {{"nodes": [
        {{"name": "prepare", "type": "Prepare", "depends": [], "meta_type": "GNode", "loop": 1}},
        {{"name": "ask", "type": "Ask", "depends": ["prepare"], "meta_type": "GNode", "loop": 1}},
        {{"name": "confirm", "type": "Confirm", "depends": ["ask"], "meta_type": "GNode", "loop": 1}},
        {{"name": "finish", "type": "Finish", "depends": ["confirm"], "meta_type": "GNode", "loop": 1}},
    ]}}
    assert node_classes is main.NODE_CLASSES
    assert node_classes == {{node.STEP_ID: node for node in main.NODES}}
    assert all(node.__name__ not in GElement.registeredClasses() for node in main.NODES)
    return real_pool(pipeline_json=pipeline_json, node_classes=node_classes, size=size)
main.WorkflowPipelinePool = checked_pool
os.environ["FLOWX_WORKFLOW_POOL_SIZE"] = "not-an-integer"
with TestClient(main.app) as client:
    pool = main.PIPELINE_POOL
    assert pool is not None
    def post(session, step, value=None):
        response = client.post("/api/run-step", json={{"sessionId": session, "stepId": step, "input": value}})
        assert response.status_code == 200, response.text
        return response.text
    assert "input_required" in post("a", "ask")
    request_a = main.SESSION_REQUESTS["a"]
    future = main.ENGINE._runs[request_a].future
    assert main.Prepare.executed == [request_a]
    assert main.Ask.executed == []
    assert 'confirm' in post("a", "ask", "Ada")
    assert main.ENGINE._runs[request_a].future is future
    assert main.Prepare.executed == [request_a]
    assert main.Ask.executed == [(request_a, "Ada")]
    assert main.ENGINE.awaiting_input(request_a) == "confirm"
    assert "input_required" in post("b", "ask")
    request_b = main.SESSION_REQUESTS["b"]
    reset = client.post("/api/reset-session", json={{"sessionId": "a"}}).json()
    assert reset["threadId"] == main.GRAPH_CONFIG_ID
    assert reset["runId"] != request_a
    assert request_a not in main.ENGINE._runs
    assert main.ENGINE.awaiting_input(request_b) == "ask"
    assert main.PIPELINE_POOL is pool and not pool.closed
    assert "confirm" in post("b", "ask", "Bob")
    assert '"confirmed":"yes"' in post("b", "confirm", "yes").replace(" ", "")
    assert request_b not in main.ENGINE._runs
    assert "confirm" in post("a", "ask", "Grace")
    assert main.Ask.executed[-1] == (reset["runId"], "Grace")
    # Leave a real native wait gate active; lifespan must cancel it before pool.close().
    assert main.ENGINE.awaiting_input(reset["runId"]) == "confirm"
assert pool.closed
assert len(pool_sources) == 1
assert main.ENGINE is None
assert main.SESSION_REQUESTS == {{}}
print("REAL_ENTRYPOINT_OK")
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REAL_ENTRYPOINT_OK" in result.stdout


def test_generated_template_runs_real_bytes_file_node(tmp_path):
    sources = {
        "Upload": '''from ag_ui_workflow import WorkflowFileNode, StepRunOutput
class Upload(WorkflowFileNode):
    STEP_ID = "upload"
    DEPENDENCIES = []
    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)
    def wait_function(self):
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            ready = isinstance(param.inputs.get(self.STEP_ID), (bytes, bytearray))
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready
    def process_input(self, file_bytes, dependency_results, session_state):
        assert isinstance(file_bytes, bytes)
        return StepRunOutput(derived={"hex": file_bytes.hex(), "length": len(file_bytes)})
''',
    }
    path = _render_backend(tmp_path, sources)
    assert MainEntryPointAuditor().audit_file(str(path)) == (True, [])
    upload_path = tmp_path / "binary.data"
    upload_path.write_bytes(b"\x00\x01\xff")
    encoded_upload = base64.b64encode(upload_path.read_bytes()).decode()
    script = f'''import importlib.util
import json
import sys
from fastapi.testclient import TestClient
sys.path.insert(0, {str(tmp_path)!r})
spec = importlib.util.spec_from_file_location("generated_file_main", {str(path)!r})
main = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = main
spec.loader.exec_module(main)
assert isinstance(main.NODES, tuple)
assert main.NODES == (main.Upload,)
assert main.NODE_CLASSES == {{"upload": main.Upload}}
assert main.STEP_CHAIN[0]["metaNodeKind"] == "WorkflowFileNode"
assert "extData" not in main.STEP_CHAIN[0]
with TestClient(main.app) as client:
    def post(session, **kwargs):
        response = client.post("/api/run-step", json={{"sessionId": session, "stepId": "upload", **kwargs}})
        assert response.status_code == 200, response.text
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert any(event.get("name") == "input_required" for event in post("path"))
    pool = main.PIPELINE_POOL
    events = post("path", file_path={str(upload_path)!r})
    assert events[-1]["result"]["final"] == {{"hex": "0001ff", "length": 3}}, events
    events = post("base64", input={{"base64": {encoded_upload!r}}})
    assert events[-1]["result"]["final"]["hex"] == "0001ff", events
    events = post("empty", input={{"base64": ""}})
    assert events[-1]["result"]["final"] == {{"hex": "", "length": 0}}, events
    assert main.PIPELINE_POOL is pool
assert pool.closed
print("REAL_FILE_NODE_OK")
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REAL_FILE_NODE_OK" in result.stdout