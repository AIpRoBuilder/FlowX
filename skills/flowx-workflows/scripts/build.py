# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-05: provide a JSON-lines SDK build and repair interface.
# Modified 2026-11-06: document the node-test pause and answer protocol in CLI help.
# Modified 2026-11-11: require a workflow folder name when allocating from the skill.
# Modified 2026-11-22: serve the contract protocol (begin_build/get_tasks/submit_task/...) as JSON lines; the
#   calling agent writes the artifacts and FlowX no longer calls an LLM.
"""Drive the FlowX workflow builder over stdin/stdout as JSON lines (the contract protocol)."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

if __name__ == "__main__":
    import plugin_settings

    _PLUGIN_WORKSPACE = plugin_settings.apply(None)  # may re-exec under the plugin's Python
else:
    _PLUGIN_WORKSPACE = None

from flowx_sdk import FlowXClient  # noqa: E402

MAX_FILE_BYTES = 2_000_000

DESCRIPTION = """\
Keep one FlowX client alive and exchange one JSON object per line. A ready event is printed first;
then every command line gets exactly one response line {"ok": true|false, "op": ..., ...}.
Build state lives in this process: keep it running for the whole build. If it exits, start it again and
send begin_build: published nodes are reused, so only unfinished work is requested again.

Commands ({"op": ..., arguments}):
  create_workflow  name?, brief?
  begin_build      workflow_id, intent?, target?, stages?, runtime_evidence?, fastapi_port?, fastapi_host?, smoke_test?
  get_build_state  workflow_id
  get_tasks        workflow_id, compact?
  submit_task      workflow_id, task_id, artifacts
  get_build_events workflow_id, since?, limit?
  reopen_stage     workflow_id, stage, step_id?, reason?
  abort_build      workflow_id, reason?
  list_workflows | list_nodes workflow_id | get_workflow workflow_id
  stop             close the client and exit

begin_build and submit_task return the open contract tasks (`next_tasks`): each has a contract, context,
and submit.slots naming the artifacts to return. For submit_task, artifacts maps slot names to text, or to
{"file": "path"} to read the text from a UTF-8 file (best for code and tests), for example:
  {"op":"submit_task","workflow_id":"w","task_id":"node:Echo:r1:a1",
   "artifacts":{"code":{"file":"/tmp/Echo.py"},"test":{"file":"/tmp/test_Echo.py"}}}
A rejection returns findings and a repair task; fix exactly those findings and submit again.
"""


def emit(stream: TextIO, **fields: Any) -> None:
    print(json.dumps(fields, ensure_ascii=False, default=str), file=stream, flush=True)


def resolve_artifacts(artifacts: Any) -> dict[str, Any]:
    """Replace ``{"file": path}`` values with the file's text."""
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object mapping slot names to text")
    resolved: dict[str, Any] = {}
    for slot, value in artifacts.items():
        if isinstance(value, dict) and set(value) == {"file"}:
            path = Path(str(value["file"])).expanduser()
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"artifact file for {slot!r} is missing or larger than {MAX_FILE_BYTES} bytes")
            resolved[slot] = path.read_text(encoding="utf-8")
        else:
            resolved[slot] = value
    return resolved


def handlers(client: FlowXClient) -> dict[str, Callable[..., Any]]:
    def submit_task(workflow_id: str, task_id: str, artifacts: Any) -> Any:
        return client.submit_task(workflow_id, task_id, resolve_artifacts(artifacts))

    def begin_build(workflow_id: str, intent: str = "", **options: Any) -> Any:
        return client.begin_build(workflow_id, intent, **{key: value for key, value in options.items() if value is not None})

    def method(name: str) -> Callable[..., Any]:
        return lambda **arguments: getattr(client, name)(**arguments)

    return {
        "create_workflow": lambda name=None, brief="": {"workflow_id": client.create_workflow(name, brief)},
        "begin_build": begin_build,
        "get_build_state": method("get_build_state"),
        "get_tasks": lambda workflow_id, compact=False: {"tasks": client.get_tasks(workflow_id, compact=compact)},
        "submit_task": submit_task,
        "get_build_events": method("get_build_events"),
        "reopen_stage": method("reopen_stage"),
        "abort_build": method("abort_build"),
        "list_workflows": lambda: {"workflows": client.workflows()},
        "list_nodes": lambda workflow_id: {"nodes": client.list_nodes(workflow_id)},
        "get_workflow": lambda workflow_id: {"workflow": client.artifacts(workflow_id)},
    }


def serve(client: FlowXClient, lines: Any, output: TextIO) -> int:
    commands = handlers(client)
    for line in lines:
        if not line.strip():
            continue
        op = ""
        try:
            command = json.loads(line)
            if not isinstance(command, dict) or not isinstance(command.get("op"), str):
                raise ValueError('expected a JSON object such as {"op": "get_build_state", "workflow_id": "..."}')
            op = command.pop("op")
            if op == "stop":
                break
            if op not in commands:
                raise ValueError(f"unknown op {op!r}; one of {sorted([*commands, 'stop'])}")
            with contextlib.redirect_stdout(sys.stderr):  # native runtime output must not reach the protocol
                result = commands[op](**command)
            emit(output, ok=True, op=op, **(result if isinstance(result, dict) else {"result": result}))
        except Exception as exc:
            emit(output, ok=False, op=op, error=str(exc), error_type=type(exc).__name__)
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--workspace",
        help="FlowX workspace; defaults to the plugin's saved workspace, then FLOWX_DEFAULT_WORKSPACE",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    output = sys.stdout
    try:
        with contextlib.redirect_stdout(sys.stderr):
            client = FlowXClient(workspace=args.workspace or _PLUGIN_WORKSPACE)
        try:
            emit(output, event="ready", workspace=str(client.workspace))
            return serve(client, sys.stdin, output)
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                client.close()
    except Exception as exc:
        emit(output, event="error", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
