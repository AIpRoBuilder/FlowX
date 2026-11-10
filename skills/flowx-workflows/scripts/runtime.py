#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Adapted the generated service HTTP and SSE protocol from flowx_mcp.server.
"""Run one generated FlowX service and exchange JSON lines over stdin/stdout."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, TextIO


DESCRIPTION = """\
Start a generated FlowX service and keep its SDK client alive for a JSON-lines session.
After GET / succeeds, stdout receives a service_ready event with base_url and session_id.
Send one JSON object per stdin line; commands are processed sequentially:
  {"action":"step","step_id":"ask","input":"answer","file_path":"optional path"}
  {"action":"reset"}
  {"action":"stop"}
For step, input and file_path are optional. Responses are JSON lines: step includes
session_id, step_id, events, awaiting, status (input_required or response_received),
and final if an SSE event supplied a result. To resume an input_required step, send
another step with awaiting.stepId and its answer; the session ID stays the same.
Reset returns the service response in reset and retains the session ID for the next
run. Inspect events and final for failures: a received response does not imply
workflow success. Invalid commands and request errors produce error events containing
operation, error and error_type (and http_status for HTTP errors); details are
sanitized. They do not end the session. Stop or stdin EOF closes the client and emits
service_stopped after cleanup. Startup/cleanup errors produce an error and a nonzero
exit. SDK output goes to stderr; service events and SDK logs may contain sensitive
data, so protect both streams.
"""


class CommandError(ValueError):
    """An invalid input line; messages are fixed and never include user data."""


class ServiceConfigurationError(ValueError):
    """The SDK did not return a usable service URL."""


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def _nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must be a non-empty string")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", required=True, type=_nonempty, help="FlowX workspace directory (defaults to the plugin's saved workspace)")
    parser.add_argument("--workflow-id", required=True, type=_nonempty, help="Generated workflow ID")
    parser.add_argument("--session-id", type=_nonempty, help="Session ID (default: a new UUID)")
    parser.add_argument("--timeout", type=_positive_float, default=1200.0,
                        help="HTTP step/reset timeout in seconds (default: 1200)")
    parser.add_argument("--ready-timeout", type=_positive_float, default=15.0,
                        help="Maximum readiness retry time in seconds (default: 15)")
    return parser


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False), file=stream, flush=True)


def _base_url(service: Any) -> str:
    if not isinstance(service, dict) or not isinstance(service.get("base_url"), str):
        raise ServiceConfigurationError("service did not provide a base URL")
    base_url = service["base_url"]
    try:
        parsed = urllib.parse.urlsplit(base_url)
        # Do not publish URL credentials, query strings, or unexpected service paths.
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or any(ord(char) < 33 or ord(char) == 127 for char in base_url)):
            raise ValueError
        _ = parsed.port  # Reject invalid or out-of-range ports.
    except ValueError:
        raise ServiceConfigurationError("service returned an invalid base URL") from None
    return base_url.rstrip("/")


def _check_service_setup(client: Any, workflow_id: str) -> Any:
    """Reject an occupied bind address and a mismatched subprocess interpreter."""
    from flowx_core.tools.agent_builder_tools import select_python_command

    if not os.path.samefile(select_python_command(), sys.executable):
        raise ServiceConfigurationError("SDK service interpreter differs; activate the matching environment")
    record = client.builder.engine.record(workflow_id)
    addresses = socket.getaddrinfo(record.fastapi_host or None, record.fastapi_port,
                                   type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    for family, kind, proto, _, address in addresses:
        with socket.socket(family, kind, proto) as probe:
            try:
                probe.bind(address)
            except OSError:
                raise ServiceConfigurationError("service bind address is unavailable") from None
            break
    return record


def _wait_until_ready(base_url: str, timeout: float, process: Any = None) -> None:
    """Retry GET / without letting a single probe exceed the remaining budget."""
    deadline = time.monotonic() + timeout
    while True:
        if process is not None and process.poll() is not None:
            raise ServiceConfigurationError("generated service exited before readiness")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("service readiness timed out")
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=min(1.0, remaining)):
                if process is not None and process.poll() is not None:
                    raise ServiceConfigurationError("generated service exited before readiness")
                return
        except OSError as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("service readiness timed out") from None
            time.sleep(min(0.2, remaining))


def _post(base_url: str, endpoint: str, payload: dict[str, Any], timeout: float) -> str:
    request = urllib.request.Request(
        f"{base_url}{endpoint}",
        data=json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise


def _sse_payloads(text: str) -> list[dict[str, Any]]:
    """Match the MCP proxy: accept each data line containing a JSON object."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                value = json.loads(line[5:].strip(), parse_constant=_reject_constant)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON number")


def _read_command(line: str) -> dict[str, Any]:
    try:
        command = json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        raise CommandError("expected a valid JSON object") from None
    if not isinstance(command, dict):
        raise CommandError("expected a JSON object")
    return command


def _step(command: dict[str, Any], base_url: str, session_id: str, timeout: float) -> dict[str, Any]:
    step_id = command.get("step_id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise CommandError("step_id must be a non-empty string")
    payload: dict[str, Any] = {"sessionId": session_id, "stepId": step_id, "input": command.get("input")}
    if "file_path" in command and command["file_path"] is not None:
        file_path = command["file_path"]
        if not isinstance(file_path, str) or not file_path.strip():
            raise CommandError("file_path must be a non-empty string or null")
        payload["file_path"] = file_path
    events = _sse_payloads(_post(base_url, "/api/run-step", payload, timeout))
    question = next((event for event in events if event.get("name") == "input_required"), None)
    response: dict[str, Any] = {
        "event": "step", "session_id": session_id, "step_id": step_id,
        "status": "input_required" if question is not None else "response_received",
        "awaiting": question.get("value") if question is not None else None,
        "events": events,
    }
    for event in reversed(events):
        if "result" in event:
            response["final"] = event["result"]
            break
    return response


def _reset(base_url: str, session_id: str, timeout: float) -> dict[str, Any]:
    body = _post(base_url, "/api/reset-session", {"sessionId": session_id}, timeout)
    return {"event": "reset", "session_id": session_id,
            "reset": json.loads(body, parse_constant=_reject_constant)}


def _error(operation: str, exc: Exception) -> dict[str, Any]:
    # Exception messages and HTTP response bodies can contain credentials or inputs.
    if isinstance(exc, (CommandError, ServiceConfigurationError)):
        message = str(exc)
    elif isinstance(exc, urllib.error.HTTPError):
        message = "HTTP request failed"
    elif isinstance(exc, TimeoutError):
        message = "service readiness timed out" if operation == "readiness" else "operation timed out"
    elif isinstance(exc, urllib.error.URLError):
        message = "could not reach the service"
    elif isinstance(exc, json.JSONDecodeError):
        message = "invalid JSON response"
    elif isinstance(exc, ImportError) and operation == "startup":
        message = "could not import the FlowX SDK or one of its dependencies"
    elif isinstance(exc, FileNotFoundError) and operation == "startup":
        message = "workflow or generated service file not found"
    else:
        message = "operation failed"
    result: dict[str, Any] = {
        "event": "error", "operation": operation, "error": message, "error_type": type(exc).__name__,
    }
    if isinstance(exc, urllib.error.HTTPError):
        result["http_status"] = exc.code
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    session_id = args.session_id or uuid.uuid4().hex
    output = sys.stdout
    phase = "startup"
    try:
        # Keep the original stream for the protocol; SDK imports, background Python
        # output, startup, and close must not corrupt JSON lines on stdout.
        with contextlib.redirect_stdout(sys.stderr):
            from flowx_sdk import FlowXClient

            with FlowXClient(workspace=args.workspace) as client:
                record = _check_service_setup(client, args.workflow_id)
                base_url = _base_url(client.run_service(args.workflow_id))
                phase = "readiness"
                _wait_until_ready(base_url, args.ready_timeout, record.process)
                _emit(output, {"event": "service_ready", "base_url": base_url,
                               "session_id": session_id, "workflow_id": args.workflow_id})
                phase = "stdin"
                for line in sys.stdin:
                    operation = "command"
                    try:
                        command = _read_command(line)
                        action = command.get("action")
                        if action == "stop":
                            break
                        if action == "step":
                            operation = "step"
                            result = _step(command, base_url, session_id, args.timeout)
                        elif action == "reset":
                            operation = "reset"
                            result = _reset(base_url, session_id, args.timeout)
                        else:
                            raise CommandError("action must be step, reset, or stop")
                        _emit(output, result)
                    except Exception as exc:
                        _emit(output, _error(operation, exc))
                phase = "shutdown"
        _emit(output, {"event": "service_stopped", "session_id": session_id})
        return 0
    except KeyboardInterrupt:
        _emit(output, {"event": "error", "operation": phase, "error": "interrupted"})
        return 130
    except Exception as exc:
        _emit(output, _error(phase, exc))
        return 1


if __name__ == "__main__":
    import plugin_settings

    _workspace = plugin_settings.apply(None)  # may re-exec under the plugin's Python
    _argv = sys.argv[1:]
    if _workspace and not any(a == "--workspace" or a.startswith("--workspace=") for a in _argv):
        _argv += ["--workspace", _workspace]
    raise SystemExit(main(_argv))