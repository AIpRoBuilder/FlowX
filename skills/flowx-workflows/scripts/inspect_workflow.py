# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-05: inspect workflow artifacts and node identifiers through the SDK.
# Modified 2026-11-23: add verify and read subcommands for integrity checks and read-only content access.
"""List workflows, nodes, read-only artifact paths/contents, integrity checks, or recovery state."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Sequence

if __name__ == "__main__":
    import plugin_settings

    _PLUGIN_WORKSPACE = plugin_settings.apply(None)  # may re-exec under the plugin's Python
else:
    _PLUGIN_WORKSPACE = None

from flowx_core.builder import WorkflowFilesModified  # noqa: E402
from flowx_sdk import FlowXClient  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="FlowX workspace; defaults to the plugin's saved workspace, then FLOWX_DEFAULT_WORKSPACE")
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("list", help="List workspace workflows")
    for name in ("nodes", "artifacts", "recovery", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--workflow-id", required=True)
    read_cmd = commands.add_parser("read", help="Read one FlowX-owned artifact as verified read-only text")
    read_cmd.add_argument("--workflow-id", required=True)
    read_cmd.add_argument("--slot", required=True, choices=("requirement", "graph", "brief", "source", "test", "markdown", "log"))
    read_cmd.add_argument("--node", help="Node name or node_id (required for source/test/markdown/log)")
    read_cmd.add_argument("--version", type=int, help="Optional workflow or node version")
    args = parser.parse_args(argv)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            with FlowXClient(workspace=args.workspace or _PLUGIN_WORKSPACE) as client:
                if args.operation == "list":
                    data = client.workflows()
                elif args.operation == "nodes":
                    data = client.list_nodes(args.workflow_id)
                elif args.operation == "artifacts":
                    data = client.artifacts(args.workflow_id)
                elif args.operation == "verify":
                    data = client.verify_workflow(args.workflow_id)
                elif args.operation == "read":
                    data = client.read_artifact(args.workflow_id, args.slot, node=args.node, version=args.version)
                else:
                    data = client.build_recovery_state(args.workflow_id)
        print(json.dumps({"ok": True, "operation": args.operation, "data": data}, ensure_ascii=False, default=str))
        return 0
    except WorkflowFilesModified as exc:
        print(json.dumps({
            "ok": False, "error": f"{type(exc).__name__}: {exc}",
            "error_code": "workflow_files_modified_outside_flowx",
            "modified": exc.modified, "remedy": exc.remedy,
        }, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())