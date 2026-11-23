# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-05: offline coverage for the SDK-backed workflow skill scripts.
# Modified 2026-11-06: cover node-pause answer help.
# Modified 2026-11-11: cover named workflow allocation and existing IDs.
# Modified 2026-11-22: cover the JSON-lines contract protocol.
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest

from flowx_sdk import FlowXClient

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "flowx-workflows" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load("build")
inspect_workflow = load("inspect_workflow")

REQUIREMENT = """## Background
Echo.

## Goals
Echo.

## Scope
One node.

## Functional Details
| node id | meta_node_kind | ext_data.type | purpose | depends |
| --- | --- | --- | --- | --- |
| Echo | WorkflowStepNode | none | Echo | - |
"""


def run(client, *commands):
    output = io.StringIO()
    lines = [command if isinstance(command, str) else json.dumps(command) for command in commands]
    assert build.serve(client, iter(lines), output) == 0
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_help_describes_the_contract_protocol(capsys):
    with pytest.raises(SystemExit) as exc:
        build.make_parser().parse_args(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    for text in ("begin_build", "submit_task", "next_tasks", '{"file": "path"}', "findings", "repair task"):
        assert text in output


def test_commands_dispatch_to_the_client():
    client = Mock()
    client.create_workflow.return_value = "wf1"
    client.get_tasks.return_value = [{"task_id": "t"}]
    client.begin_build.return_value = {"status": "running", "next_tasks": []}

    responses = run(
        client,
        {"op": "create_workflow", "name": "wf1", "brief": "b"},
        {"op": "begin_build", "workflow_id": "wf1", "intent": "do it", "target": "workflow", "stages": None},
        {"op": "get_tasks", "workflow_id": "wf1", "compact": True},
        {"op": "abort_build", "workflow_id": "wf1", "reason": "stop"},
        {"op": "stop"},
        {"op": "get_tasks", "workflow_id": "never"},
    )

    assert responses[0] == {"ok": True, "op": "create_workflow", "workflow_id": "wf1"}
    assert responses[1]["status"] == "running"
    client.create_workflow.assert_called_once_with("wf1", "b")
    client.begin_build.assert_called_once_with("wf1", "do it", target="workflow")
    assert responses[2]["tasks"] == [{"task_id": "t"}]
    client.get_tasks.assert_called_once_with("wf1", compact=True)
    client.abort_build.assert_called_once_with(workflow_id="wf1", reason="stop")
    assert len(responses) == 4  # nothing runs after stop


@pytest.mark.parametrize("line, fragment", [
    ("not json", "Expecting value"),
    ("[1]", "expected a JSON object"),
    ('{"op": "nope"}', "unknown op"),
    ('{"op": "get_build_state"}', "workflow_id"),
])
def test_bad_commands_get_an_error_line_and_do_not_end_the_session(line, fragment):
    responses = run(create_autospec(FlowXClient, instance=True), line, {"op": "list_workflows"})

    assert responses[0]["ok"] is False and fragment in responses[0]["error"]
    assert responses[1]["ok"] is True


def test_file_artifacts_are_read_as_text(tmp_path):
    code = tmp_path / "Echo.py"
    code.write_text("print('é')\n", encoding="utf-8")

    assert build.resolve_artifacts({"code": {"file": str(code)}, "markdown": "inline"}) == {
        "code": "print('é')\n", "markdown": "inline",
    }
    with pytest.raises(ValueError, match="missing"):
        build.resolve_artifacts({"code": {"file": str(tmp_path / "absent.py")}})
    with pytest.raises(ValueError, match="mapping"):
        build.resolve_artifacts("code")


def test_a_real_client_round_trip(tmp_path):
    import flowx_sdk

    requirement = tmp_path / "requirement.md"
    requirement.write_text(REQUIREMENT, encoding="utf-8")
    with flowx_sdk.FlowXClient(workspace=tmp_path / "ws") as client:
        responses = run(
            client,
            {"op": "create_workflow", "name": "echo"},
            {"op": "begin_build", "workflow_id": "echo", "intent": "Echo"},
            {"op": "submit_task", "workflow_id": "echo", "task_id": "requirement:requirement:r1:a1",
             "artifacts": {"requirement": {"file": str(requirement)}}},
            {"op": "get_build_state", "workflow_id": "echo"},
        )

    assert responses[1]["next_tasks"][0]["task_id"] == "requirement:requirement:r1:a1"
    assert responses[2]["accepted"] is True and responses[2]["next_tasks"][0]["stage"] == "graph"
    assert responses[3]["next_action"]["kind"] == "submit_task"


def test_main_keeps_sdk_output_off_json_stdout(monkeypatch, capsys):
    class Client:
        workspace = Path("/tmp/ws")

        def __init__(self, *, workspace):
            print("constructor output")

        def create_workflow(self, name, brief):
            print("SDK output")
            return name

        def close(self):
            print("close output")

    monkeypatch.setattr(build, "FlowXClient", Client)
    monkeypatch.setattr(build.sys, "stdin", io.StringIO('{"op": "create_workflow", "name": "wf"}\n{"op": "stop"}\n'))

    assert build.main([]) == 0
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert lines[0] == {"event": "ready", "workspace": "/tmp/ws"}
    assert lines[1] == {"ok": True, "op": "create_workflow", "workflow_id": "wf"}
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
