# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-05: provide a JSON-lines SDK build and repair interface.
# Modified 2026-11-06: document the node-test pause and answer protocol in CLI help.
"""Build and evolve FlowX workflows; exchange repair answers over stdin as JSON lines."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

if __name__ == "__main__":
    import plugin_settings

    _PLUGIN_WORKSPACE = plugin_settings.apply(None)  # may re-exec under the plugin's Python
else:
    _PLUGIN_WORKSPACE = None

from flowx_sdk import FlowXClient, result_to_dict  # noqa: E402


REPAIR_HELP = """\
Node-test repair pauses:
    When an input_required event appears, inspect awaiting.prompt and
    awaiting.test_log. Keep this process running and send one JSON line to stdin:
        {"action":"answer","input":"Handle empty input"}
        {"action":"answer","input":"test: correct the fixture"}
        {"action":"answer","input":"skip"}
        {"action":"answer","input":""}
        {"action":"abort"}
    Send one answer per pause; test: targets the test, skip skips this attempt,
    and empty input uses the test log alone. EOF aborts the paused build.
    Audit violations are repaired automatically; they do not have a separate pause.
"""


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str), flush=True)


def sdk_call(function: Any, *args: Any, **kwargs: Any) -> Any:
    with contextlib.redirect_stdout(sys.stderr):
        return function(*args, **kwargs)


def text_options(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--prompt", help="Requirement or change as a single argument")
    group.add_argument("--prompt-file", type=Path, help="UTF-8 file containing the requirement or change")


def build_options(parser: argparse.ArgumentParser, *, cascade: bool = False, default_cascade: bool = True) -> None:
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--max-retries", type=int, default=1, help="Extra attempts per stage")
    parser.add_argument("--auto-repair", action="store_true", help="Repair failing node tests without asking")
    if cascade:
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--cascade", dest="cascade", action="store_true", help="Rebuild dependent stages")
        group.add_argument("--no-cascade", dest="cascade", action="store_false", help="Do not rebuild dependent stages")
        parser.set_defaults(cascade=default_cascade)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, epilog=REPAIR_HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", help="FlowX workspace; defaults to the plugin's saved workspace, then FLOWX_DEFAULT_WORKSPACE")
    commands = parser.add_subparsers(dest="operation", required=True)

    commands.add_parser("allocate", help="Allocate an empty workflow id")
    new = commands.add_parser("new", help="Allocate and build a complete workflow")
    new.add_argument("--workflow-id", help="Previously allocated empty workflow id")
    text_options(new)
    build_options(new, cascade=True)
    new.add_argument("--host", default="0.0.0.0")
    new.add_argument("--port", type=int, default=8000)
    new.add_argument("--smoke-test", action="store_true")

    graph = commands.add_parser("graph", help="Create or modify only graph and requirements")
    graph.add_argument("--workflow-id", help="Allocated or existing id; omit to allocate")
    text_options(graph)
    build_options(graph)

    nodes = commands.add_parser("nodes", help="Build nodes or amend selected step ids", epilog=REPAIR_HELP,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    nodes.add_argument("--workflow-id", required=True)
    nodes.add_argument("--amend", nargs=2, action="append", metavar=("STEP_ID", "GUIDANCE"), default=[])
    build_options(nodes, cascade=True, default_cascade=False)

    main = commands.add_parser("main", help="Generate or amend the main entrypoint")
    main.add_argument("--workflow-id", required=True)
    build_options(main)
    main.add_argument("--host", default="0.0.0.0")
    main.add_argument("--port", type=int, default=8000)
    main.add_argument("--smoke-test", action="store_true")

    evolve = commands.add_parser("evolve", help="Evolve a workflow or selected node")
    evolve.add_argument("--workflow-id", required=True)
    evolve.add_argument("--target", help="workflow, a step id, or a node id; omit for SDK selection")
    text_options(evolve)
    build_options(evolve, cascade=True)

    repair = commands.add_parser("repair", help="Repair from retained errors and/or log files")
    repair.add_argument("--workflow-id", required=True)
    text_options(repair, required=False)
    repair.add_argument("--fix-errors", action="store_true")
    repair.add_argument("--log-file", type=Path, action="append", default=[])
    build_options(repair, cascade=True)
    return parser


def prompt_text(args: argparse.Namespace) -> str:
    file = getattr(args, "prompt_file", None)
    return file.read_text(encoding="utf-8") if file else (getattr(args, "prompt", None) or "")


def run_operation(client: FlowXClient, args: argparse.Namespace) -> Any:
    if args.operation == "allocate":
        workflow_id = sdk_call(client.create_workflow)
        emit("allocated", workflow_id=workflow_id)
        return None

    prompt = prompt_text(args)
    if args.operation in {"new", "graph", "evolve"} and not prompt.strip():
        raise ValueError("a non-empty prompt is required")
    if args.operation == "repair" and not (prompt.strip() or args.fix_errors or args.log_file):
        raise ValueError("repair requires a prompt, --fix-errors, or --log-file")

    options: dict[str, Any] = {
        "temperature": args.temperature,
        "max_retries": args.max_retries,
        "await_repair_input": not args.auto_repair,
    }
    if hasattr(args, "cascade"):
        options["cascade"] = args.cascade
    if args.operation in {"new", "nodes", "evolve", "repair"}:
        options["node_options"] = {"temperature": args.temperature, "python_command": sys.executable}

    workflow_id = args.workflow_id
    if args.operation in {"new", "graph"} and not workflow_id:
        workflow_id = sdk_call(client.create_workflow)
        emit("allocated", workflow_id=workflow_id)
    if args.operation in {"nodes", "evolve", "repair"} and args.cascade:
        # The SDK defaults would otherwise reset an existing local binding to 0.0.0.0.
        record = sdk_call(lambda: client.builder.engine.record(workflow_id))
        options.update(fastapi_host=record.fastapi_host, fastapi_port=record.fastapi_port)
    if getattr(args, "smoke_test", False):
        from flowx_core.tools.agent_builder_tools import select_python_command
        if not os.path.samefile(select_python_command(), sys.executable):
            raise RuntimeError("SDK service interpreter differs from this interpreter; activate the matching environment")
    emit("started", operation=args.operation, workflow_id=workflow_id)

    if args.operation == "new":
        return sdk_call(client.build_new_workflow, workflow_id, prompt, fastapi_host=args.host, fastapi_port=args.port,
                smoke_test_main=args.smoke_test, **options)
    if args.operation == "graph":
        return sdk_call(client.build_allocated_workflow_json, workflow_id, prompt, **options)
    if args.operation == "nodes":
        amendments = dict(args.amend)
        if len(amendments) != len(args.amend):
            raise ValueError("each --amend step id must be unique")
        return sdk_call(client.build_nodes, workflow_id, amendments or None, **options)
    if args.operation == "main":
        return sdk_call(client.build_main_entrypoint, workflow_id, fastapi_host=args.host,
                fastapi_port=args.port, smoke_test_main=args.smoke_test, **options)
    if args.operation == "evolve":
        return sdk_call(client.evolve, workflow_id, prompt, target=args.target, **options)
    logs = [file.read_text(encoding="utf-8") for file in args.log_file]
    return sdk_call(client.build_workflow, prompt, workflow_id=workflow_id, logs=logs, fix_errors=args.fix_errors, **options)


def finish(client: FlowXClient, result: Any) -> int:
    """Keep the SDK client alive across all answers; abort if the input channel closes."""
    while result.status == "input_required":
        awaiting = dict(result.awaiting or {})
        request_id = result.request_id
        step_id = awaiting.get("stepId")
        emit("input_required", workflow_id=result.workflow_id, request_id=request_id, awaiting=awaiting)
        if not request_id or not isinstance(step_id, str) or not step_id:
            raise RuntimeError("paused build did not provide a request id and stepId")
        while True:
            line = sys.stdin.readline()
            if not line:
                sdk_call(client.abort, request_id)
                emit("aborted", workflow_id=result.workflow_id, reason="stdin closed")
                return 1
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("action") not in ("answer", "abort"):
                    raise ValueError('expected {"action":"answer","input":"..."} or {"action":"abort"}')
                if message.get("action") == "abort":
                    break
                answer = message.get("input", "")
                if not isinstance(answer, str):
                    raise ValueError("answer input must be a string")
                if "step_id" in message and message["step_id"] != step_id:
                    raise ValueError("step_id does not match the current repair question")
            except (ValueError, json.JSONDecodeError) as exc:
                emit("error", error=str(exc))
                continue
            result = sdk_call(client.answer, request_id, answer, step_id)
            break
        if message.get("action") == "abort":
            sdk_call(client.abort, request_id)
            emit("aborted", workflow_id=result.workflow_id, reason="requested")
            return 1
    emit("result", result=result_to_dict(result))
    return 0 if result.status == "completed" and result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        # Native generation may print: reserve stdout for the JSON-lines protocol.
        with contextlib.redirect_stdout(sys.stderr):
            client = FlowXClient(workspace=args.workspace or _PLUGIN_WORKSPACE)
        try:
            result = run_operation(client, args)
            return 0 if result is None else finish(client, result)
        finally:
            with contextlib.redirect_stdout(sys.stderr):
                client.close()
    except Exception as exc:
        emit("error", error=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())