"""Offline coverage for the template-backed Coder subclass."""

import json
from types import SimpleNamespace

import pytest
from ag_ui_workflow import StepRunOutput

from flowx_core.llm_client.coder import Coder
from flowx_core.builder_services import MainEntrypointService
from flowx_core.builder_support import AuditRepairLoop
from flowx_core.worker.main_writer import PromptMainFileCoder


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


def test_process_input_writes_then_amends_from_session_request(tmp_path) -> None:
    writer = PromptMainFileCoder()
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
        }},
    )
    assert output.derived["main_entrypoint_path"] == str(main_path)
    assert "Collect.step_meta()" in main_path.read_text(encoding="utf-8")

    (package_dir / "Publish.py").write_text("class Publish: pass\n", encoding="utf-8")
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}, {"name": "Publish"}]}), encoding="utf-8")
    result = writer.process_input("", {}, {"PromptMainFileCoder::action": "amend", "main_entrypoint": {
        "output_path": str(main_path), "amendment": "Refresh imports",
    }})
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


def test_builder_service_routes_generation_and_audit_repair_through_process_input(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "nodes"
    package_dir.mkdir()
    (package_dir / "Collect.py").write_text("class Collect: pass\n", encoding="utf-8")
    graph_path = tmp_path / "workflow.json"
    graph_path.write_text(json.dumps({"nodes": [{"name": "Collect"}]}), encoding="utf-8")

    writer = PromptMainFileCoder()
    requests = []
    original_process_input = writer.process_input

    def capture_request(user_input, dependency_results, session_state):
        requests.append((session_state["PromptMainFileCoder::action"], dict(session_state["main_entrypoint"])))
        return original_process_input(user_input, dependency_results, session_state)

    monkeypatch.setattr(writer, "process_input", capture_request)
    audit_calls = []

    def audit_main_entrypoint_file(*_args):
        audit_calls.append(None)
        if len(audit_calls) == 1:
            return False, [SimpleNamespace(lineno=1, rule="missing", detail="refresh")]
        return True, []

    logger = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    builder = SimpleNamespace(
        root_dir=str(tmp_path),
        graph_plan_path=str(graph_path),
        main_output_path=None,
        main_writer=writer,
        main_entry_auditor=SimpleNamespace(audit_main_entrypoint_file=audit_main_entrypoint_file),
        _logger=logger,
        _make_audit_repair_loop=lambda: AuditRepairLoop(logger=logger, max_attempts=2),
    )

    output = MainEntrypointService(builder).write_main_entrypoint(output_filename="nodes/main.py")

    assert output == str(package_dir / "main.py")
    assert [action for action, _request in requests] == ["write", "amend"]
    assert requests[0][1]["graph_plan_json_path"] == str(graph_path)
    assert "missing" in requests[1][1]["amendment"]
    assert "Collect.step_meta()" in (package_dir / "main.py").read_text(encoding="utf-8")