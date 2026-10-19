# SPDX-License-Identifier: Apache-2.0
"""Load the MCP server in isolation: stubbed MCP SDK, bootstrap and FlowX SDK; no core, pools or network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class FakeMCP:
    def __init__(self, *_args, **_kwargs):
        self.tools = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function
        return register


class FakeClient:
    """Records engine calls; results are plain dicts, which the fake ``result_to_dict`` passes through."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.results: dict[str, Any] = {}
        self.closed = False

    def _record(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        outcome = self.results.get(name, {"ok": True, "workflow_id": "wf1"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def build_workflow_json(self, *args, **kwargs):
        return self._record("build_workflow_json", *args, **kwargs)

    def build_nodes(self, *args, **kwargs):
        return self._record("build_nodes", *args, **kwargs)

    def build_main_entrypoint(self, *args, **kwargs):
        return self._record("build_main_entrypoint", *args, **kwargs)

    def build_workflow(self, *args, **kwargs):
        return self._record("build_workflow", *args, **kwargs)

    def evolve(self, *args, **kwargs):
        return self._record("evolve", *args, **kwargs)

    def list_nodes(self, *args):
        return self._record("list_nodes", *args)

    def run_service(self, *args, **kwargs):
        return self._record("run_service", *args, **kwargs)

    def stop_service(self, *args, **kwargs):
        return self._record("stop_service", *args, **kwargs)

    def workflows(self):
        return self._record("workflows")

    def artifacts(self, *args):
        return self._record("artifacts", *args)

    def close(self):
        self.closed = True


@pytest.fixture
def mcp_server(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "flowx_mcp"
    package_name = "_flowx_mcp_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(source)]
    monkeypatch.setitem(sys.modules, package_name, package)

    bootstrap = ModuleType(f"{package_name}.bootstrap")
    bootstrap.bootstrap_import_paths = lambda *_args: None
    bootstrap.load_env_from_runtime_context = lambda *_args: None
    monkeypatch.setitem(sys.modules, bootstrap.__name__, bootstrap)

    sdk = ModuleType("mcp.server.fastmcp")
    sdk.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, sdk.__name__, sdk)

    client = FakeClient()
    flowx_sdk = ModuleType("flowx_sdk")
    flowx_sdk.FlowXClient = lambda: client
    flowx_sdk.result_to_dict = lambda result: result
    monkeypatch.setitem(sys.modules, "flowx_sdk", flowx_sdk)

    spec = importlib.util.spec_from_file_location(f"{package_name}.server", source / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module._MCP_SERVER_AVAILABLE, "the MCP SDK entry point must resolve to its stub"
    return SimpleNamespace(module=module, client=client, tools=module.create_server().tools)
