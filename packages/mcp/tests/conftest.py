# SPDX-License-Identifier: Apache-2.0
"""Isolated MCP modules: no core imports, native pools, SDK servers or network."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
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


@pytest.fixture
def mcp_modules(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "flowx_mcp"
    package_name = "_flowx_mcp_lifecycle_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(source)]
    monkeypatch.setitem(sys.modules, package_name, package)

    bootstrap = ModuleType(f"{package_name}.bootstrap")
    bootstrap.bootstrap_import_paths = lambda *_args: None
    bootstrap.load_env_from_runtime_context = lambda *_args: None
    monkeypatch.setitem(sys.modules, bootstrap.__name__, bootstrap)

    registry = ModuleType(f"{package_name}.registry")
    registry.core_available = lambda: False
    registry.core_import_error = lambda: "core intentionally stubbed"
    registry.find_free_port = lambda: 8765
    registry.registry = object()
    monkeypatch.setitem(sys.modules, registry.__name__, registry)

    sdk = ModuleType("mcp.server.fastmcp")
    sdk.FastMCP = FakeMCP
    monkeypatch.setitem(sys.modules, sdk.__name__, sdk)

    loaded = []
    for name in ("client", "server"):
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", source / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    return tuple(loaded)


@pytest.fixture
def fake_http(mcp_modules, monkeypatch):
    client, _ = mcp_modules
    requests = []
    responses: list[Any] = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        assert responses, "Unexpected HTTP request (network is disabled)"
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            body = ": heartbeat\r\n\r\n" + "".join(
                f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
                for event in response
            )
            return io.BytesIO(body.encode("utf-8"))
        if isinstance(response, bytes):
            return io.BytesIO(response)
        return io.BytesIO(json.dumps(response).encode("utf-8"))

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    return requests, responses