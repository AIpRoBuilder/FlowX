# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-05: offline coverage for the SDK-backed workflow skill scripts.
# Modified 2026-11-06: cover node-pause answer help.
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "flowx-workflows" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build")
inspect_workflow = load("inspect_workflow")


def result(*, status="completed", ok=True, request_id="", awaiting=None):
    from flowx_core.agent_workflow_engine import AgentResult
    return AgentResult(workflow_id="wf1", ok=ok, intent=None, stages={}, errors={} if ok else {"nodes": "failed"},
                       logs={}, artifacts={}, status=status, request_id=request_id, awaiting=awaiting)


def events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize("arguments", [["--help"], ["nodes", "--help"]])
def test_help_explains_node_pause_answers(arguments, capsys):
    with pytest.raises(SystemExit) as exc:
        build.make_parser().parse_args(arguments)
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert '"action":"answer"' in output
    assert '"action":"abort"' in output
    assert "awaiting.test_log" in output
    assert "Audit violations are repaired automatically" in output


@pytest.mark.parametrize("arguments,method,expected", [
    (["new", "--prompt", "hi", "--host", "127.0.0.1", "--port", "8123"], "build_new_workflow", ("wf1", "hi")),
    (["graph", "--prompt", "hi"], "build_allocated_workflow_json", ("wf1", "hi")),
    (["nodes", "--workflow-id", "wf1", "--amend", "s1", "fix", "--amend", "s2", "change"], "build_nodes", ("wf1", {"s1": "fix", "s2": "change"})),
    (["main", "--workflow-id", "wf1"], "build_main_entrypoint", ("wf1",)),
    (["evolve", "--workflow-id", "wf1", "--target", "s1", "--prompt", "change"], "evolve", ("wf1", "change")),
    (["repair", "--workflow-id", "wf1", "--fix-errors"], "build_workflow", ("",)),
])
def test_dispatch(arguments, method, expected, capsys):
    client = Mock()
    client.create_workflow.return_value = "wf1"
    client.builder.engine.record.return_value = SimpleNamespace(fastapi_host="127.0.0.1", fastapi_port=9000)
    args = build.make_parser().parse_args(arguments)
    build.run_operation(client, args)
    call = getattr(client, method).call_args
    assert call.args == expected
    assert call.kwargs["await_repair_input"] is True
    assert call.kwargs["max_retries"] == 1
    if method in {"evolve", "build_workflow"}:
        assert call.kwargs["fastapi_host"] == "127.0.0.1" and call.kwargs["fastapi_port"] == 9000
    if method == "build_nodes":
        assert call.kwargs["cascade"] is False
    if method == "evolve":
        assert call.kwargs["target"] == "s1"
    if method == "build_workflow":
        assert call.kwargs["fix_errors"] is True and call.kwargs["workflow_id"] == "wf1"
    assert events(capsys)[-1]["event"] == "started"


def test_files_options_and_no_cascade(tmp_path, capsys):
    prompt = tmp_path / "requirement.txt"
    prompt.write_text("long requirement", encoding="utf-8")
    log = tmp_path / "run.log"
    log.write_text("trace", encoding="utf-8")
    client = Mock()
    args = build.make_parser().parse_args([
        "repair", "--workflow-id", "wf1", "--prompt-file", str(prompt), "--log-file", str(log),
        "--auto-repair", "--no-cascade", "--max-retries", "3",
    ])
    build.run_operation(client, args)
    client.build_workflow.assert_called_once_with("long requirement", workflow_id="wf1", logs=["trace"],
                                                   fix_errors=False, cascade=False, temperature=0.35,
                                                   max_retries=3, await_repair_input=False,
                                                   node_options={"temperature": 0.35, "python_command": build.sys.executable})
    assert events(capsys)[0]["workflow_id"] == "wf1"


def test_guided_repair_handles_invalid_answer_and_multiple_pauses(monkeypatch, capsys):
    pending1 = result(status="input_required", request_id="r1", awaiting={"stepId": "s1", "test_log": "failure"})
    pending2 = result(status="input_required", request_id="r1", awaiting={"stepId": "s2"})
    client = Mock()
    client.answer.side_effect = [pending2, result()]
    monkeypatch.setattr(build.sys, "stdin", io.StringIO(
        'invalid\n{"action":"answer","step_id":"wrong"}\n'
        '{"action":"answer","input":"test: fix fixture"}\n{"action":"answer","input":""}\n'))
    assert build.finish(client, pending1) == 0
    assert [call.args for call in client.answer.call_args_list] == [("r1", "test: fix fixture", "s1"), ("r1", "", "s2")]
    output = events(capsys)
    assert [item["event"] for item in output] == ["input_required", "error", "error", "input_required", "result"]
    assert output[-1]["result"]["ok"] is True


@pytest.mark.parametrize("answer", ['', '{"action":"abort"}\n'])
def test_abort_and_eof_release_pending(monkeypatch, capsys, answer):
    client = Mock()
    monkeypatch.setattr(build.sys, "stdin", io.StringIO(answer))
    assert build.finish(client, result(status="input_required", request_id="r1", awaiting={"stepId": "s"})) == 1
    client.abort.assert_called_once_with("r1")
    assert events(capsys)[-1]["event"] == "aborted"


def test_failed_build_is_not_success(capsys):
    assert build.finish(Mock(), result(ok=False)) == 1
    assert events(capsys)[0]["result"]["errors"] == {"nodes": "failed"}


def test_abort_failure_is_not_treated_as_invalid_answer(monkeypatch, capsys):
    client = Mock()
    client.abort.side_effect = ValueError("cleanup failed")
    monkeypatch.setattr(build.sys, "stdin", io.StringIO('{"action":"abort"}\n'))
    with pytest.raises(ValueError, match="cleanup failed"):
        build.finish(client, result(status="input_required", request_id="r1", awaiting={"stepId": "s"}))
    assert [event["event"] for event in events(capsys)] == ["input_required"]


def test_main_keeps_sdk_output_off_json_stdout(monkeypatch, capsys):
    class Client:
        def __init__(self, *, workspace):
            print("constructor output")
        def create_workflow(self):
            print("SDK output")
            return "wf1"
        def close(self):
            print("close output")
    monkeypatch.setattr(build, "FlowXClient", Client)
    assert build.main(["allocate"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"event": "allocated", "workflow_id": "wf1"}
    assert "SDK output" in captured.err and "close output" in captured.err


def test_inspection_dispatch_and_close(monkeypatch, capsys):
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.list_nodes.return_value = [{"step_id": "s1"}]
    monkeypatch.setattr(inspect_workflow, "FlowXClient", lambda **kwargs: client)
    assert inspect_workflow.main(["--workspace", "/tmp/work", "nodes", "--workflow-id", "wf1"]) == 0
    client.list_nodes.assert_called_once_with("wf1")
    client.__exit__.assert_called_once()
    assert events(capsys)[0]["data"] == [{"step_id": "s1"}]