# SPDX-License-Identifier: Apache-2.0
"""Real MCP ownership paths with four eager stub pools, never native resources."""

import importlib.util
import json
import sys
from itertools import count
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class StubPool:
    def __init__(self):
        self.close_calls = 0
        self.fail = False

    def close(self):
        self.close_calls += 1
        if self.fail:
            raise RuntimeError("pool cleanup failed")


class StubBuilder:
    instances = []

    def __init__(self, **config):
        self.config = config
        self.root_dir = config["root_dir"]
        self.pools = [StubPool() for _ in range(4)]
        self.actions = []
        self.close_calls = 0
        self.backend_server_process = None
        self.graph_plan_path = self.workflow_json_path = ""
        self.requirement_md_path = self.main_output_path = ""
        self.instances.append(self)

    def reset_llm_config(self, **config):
        self.config.update({key: value for key, value in config.items() if value is not None})

    def _stop_managed_server_process(self, _process):
        self.actions.append("stop")

    def close(self):
        self.actions.append("close")
        self.close_calls += 1
        error = None
        for pool in self.pools:
            try:
                pool.close()
            except RuntimeError as exc:
                error = error or exc
        if error is not None:
            raise error

    def _load_planned_graph(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def generate_main_entrypoint(self, *_args, **_kwargs):
        self.main_output_path = str(Path(self.root_dir) / "main.py")
        return self.main_output_path

    def _sync_workflow_graph_json(self, **_kwargs):
        return self.workflow_json_path or self.graph_plan_path


class StubProcess:
    pid = 12345

    def __init__(self, *, fail_poll=False):
        self.fail_poll = fail_poll
        self.stopped = False

    def poll(self):
        if self.fail_poll:
            raise RuntimeError("backend poll failed")
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, **_kwargs):
        return 0

    def kill(self):
        self.stopped = True


@pytest.fixture
def registry_module(mcp_modules, monkeypatch):
    """Load registry like conftest's isolated server, but with a stub core import."""
    client, server = mcp_modules
    core = ModuleType("flowx_core")
    core.__path__ = []
    builder_module = ModuleType("flowx_core.agent_builder")
    builder_module.AgentBuilder = StubBuilder
    monkeypatch.setitem(sys.modules, core.__name__, core)
    monkeypatch.setitem(sys.modules, builder_module.__name__, builder_module)
    monkeypatch.setattr(StubBuilder, "instances", [])

    source = Path(__file__).resolve().parents[1] / "flowx_mcp" / "registry.py"
    spec = importlib.util.spec_from_file_location(f"{server.__package__}.registry", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    session_ids = count(1)
    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex=f"{next(session_ids):012x}"))
    monkeypatch.setattr(module, "_default_llm_config", lambda: {
        "api_key": "stub", "model": "stub-model", "provider": "stub-provider",
    })
    monkeypatch.setattr(server, "registry", module.registry)
    monkeypatch.setattr(server, "health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(client, "health_check", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_select_python_command", lambda: "stub-python")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: StubProcess())

    def no_network(*_args, **_kwargs):
        pytest.fail("Unexpected network request")

    monkeypatch.setattr(client.urllib.request, "urlopen", no_network)
    return module


def create_handle(module, workspace, name="example"):
    handle = module.registry.get_or_create(workspace=str(workspace), workflow_name=name)
    handle.backend_port = 8765
    return handle


def write_graph(workspace, *, content='{"nodes": []}'):
    root = workspace / "example"
    root.mkdir(parents=True, exist_ok=True)
    (root / "workflow.json").write_text(content, encoding="utf-8")
    (root / "main.py").write_text("# stub backend\n", encoding="utf-8")


def assert_disposed(builder):
    assert builder.actions == ["stop", "close"]
    assert builder.close_calls == 1
    assert [pool.close_calls for pool in builder.pools] == [1, 1, 1, 1]


def test_pop_transfers_ownership_without_stopping_or_closing(registry_module, tmp_path):
    module = registry_module
    handle = create_handle(module, tmp_path)
    handle.is_running = True
    assert module.registry.pop("example", workspace=str(tmp_path)) is handle
    assert module.registry.get("example", workspace=str(tmp_path)) is None
    assert handle.is_running is True
    assert handle.builder.actions == []
    handle.close()
    handle.close()
    assert_disposed(handle.builder)


def test_creation_status_distinguishes_new_and_shared_handles(registry_module, tmp_path):
    module = registry_module
    handle, created = module.registry.get_or_create_with_status(
        workspace=str(tmp_path), workflow_name="example",
    )
    shared, shared_created = module.registry.get_or_create_with_status(
        workspace=str(tmp_path), workflow_name="example", model="refreshed-model",
    )
    assert created is True
    assert shared_created is False
    assert shared is handle
    assert len(StubBuilder.instances) == 1
    assert shared.model == "refreshed-model"
    assert shared.builder.actions == []


def test_kill_unregisters_and_disposes_once(registry_module, tmp_path):
    module = registry_module
    handle = create_handle(module, tmp_path)
    handle.is_running = True
    handle.session_id = "retained"
    handle.completed_steps = ["Entry"]
    assert module.registry.kill_workflow(
        "example", backend_port=8765, workspace=str(tmp_path),
    ) is handle
    assert module.registry.list_workflows() == []
    assert handle.is_running is False
    assert handle.session_id == "retained"
    assert handle.completed_steps == ["Entry"]
    assert_disposed(handle.builder)
    handle.close()
    assert_disposed(handle.builder)
    with pytest.raises(KeyError):
        module.registry.kill_workflow("example", backend_port=8765, workspace=str(tmp_path))
    assert_disposed(handle.builder)


@pytest.mark.parametrize("port", [0, 8766])
def test_rejected_kill_retains_live_builder(registry_module, tmp_path, port):
    module = registry_module
    handle = create_handle(module, tmp_path)
    with pytest.raises(ValueError):
        module.registry.kill_workflow("example", backend_port=port, workspace=str(tmp_path))
    assert module.registry.get("example", workspace=str(tmp_path)) is handle
    assert handle.builder.actions == []


def test_discard_is_identity_checked_terminal_removal(registry_module, tmp_path):
    module = registry_module
    old = create_handle(module, tmp_path)
    assert module.registry.pop("example", workspace=str(tmp_path)) is old
    replacement = create_handle(module, tmp_path)
    assert module.registry.discard(old) is False
    assert old.builder.actions == []  # pop transferred ownership to this caller.
    assert module.registry.get("example", workspace=str(tmp_path)) is replacement
    assert module.registry.discard(replacement) is True
    assert module.registry.discard(replacement) is False
    assert_disposed(replacement.builder)
    old.close()
    assert_disposed(old.builder)


@pytest.mark.parametrize("fail_stop,fail_pool", [(True, False), (False, True), (True, True)])
def test_close_attempts_both_cleanups_and_stays_terminal_after_failure(
    registry_module, tmp_path, fail_stop, fail_pool,
):
    handle = create_handle(registry_module, tmp_path)
    handle.is_running = True
    handle.backend_process = StubProcess(fail_poll=fail_stop)
    handle.builder.pools[0].fail = fail_pool
    with pytest.raises(RuntimeError, match="pool cleanup failed" if fail_pool else "backend poll failed"):
        handle.close()
    assert handle.backend_process is None
    assert handle.is_running is False
    assert_disposed(handle.builder)
    handle.close()
    assert_disposed(handle.builder)
    with pytest.raises(RuntimeError, match="closed"):
        handle.start_backend()
    with pytest.raises(RuntimeError, match="closed"):
        handle.reload_backend()
    assert_disposed(handle.builder)


def test_close_is_reentrant_during_builder_cleanup(registry_module, tmp_path, monkeypatch):
    handle = create_handle(registry_module, tmp_path)
    close_builder = handle.builder.close

    def reentrant_close():
        handle.close()
        close_builder()

    monkeypatch.setattr(handle.builder, "close", reentrant_close)
    handle.close()
    assert_disposed(handle.builder)


def test_kill_cleanup_failure_still_unregisters_handle(registry_module, tmp_path):
    module = registry_module
    handle = create_handle(module, tmp_path)
    handle.builder.pools[0].fail = True
    with pytest.raises(RuntimeError, match="pool cleanup failed"):
        module.registry.kill_workflow("example", backend_port=8765, workspace=str(tmp_path))
    assert module.registry.get("example", workspace=str(tmp_path)) is None
    handle.close()
    assert_disposed(handle.builder)


def test_missing_graph_is_validated_before_allocating_builder(registry_module, mcp_modules, tmp_path):
    _, server = mcp_modules
    (tmp_path / "example").mkdir()
    with pytest.raises(KeyError, match="missing"):
        server._attach_existing_workflow("example", workspace=str(tmp_path))
    assert registry_module.registry.list_workflows() == []
    assert StubBuilder.instances == []


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_failed_new_attachment_disposes_and_preserves_load_error(
    registry_module, mcp_modules, tmp_path, monkeypatch, fail_cleanup,
):
    _, server = mcp_modules
    write_graph(tmp_path, content="invalid json")
    load = StubBuilder._load_planned_graph

    def failing_load(builder, path):
        builder.pools[0].fail = fail_cleanup
        return load(builder, path)

    monkeypatch.setattr(StubBuilder, "_load_planned_graph", failing_load)
    with pytest.raises(json.JSONDecodeError):
        server._attach_existing_workflow("example", workspace=str(tmp_path))
    assert registry_module.registry.list_workflows() == []
    assert len(StubBuilder.instances) == 1
    assert_disposed(StubBuilder.instances[0])


def test_attachment_rollback_does_not_remove_or_close_a_replacement(
    registry_module, mcp_modules, tmp_path, monkeypatch,
):
    _, server = mcp_modules
    write_graph(tmp_path)
    transferred = []

    def replace_during_load(builder, _path):
        transferred.append(registry_module.registry.pop("example", workspace=str(tmp_path)))
        assert transferred[0].builder is builder
        create_handle(registry_module, tmp_path)
        raise ValueError("attachment superseded")

    monkeypatch.setattr(StubBuilder, "_load_planned_graph", replace_during_load)
    with pytest.raises(ValueError, match="attachment superseded"):
        server._attach_existing_workflow("example", workspace=str(tmp_path))
    assert len(StubBuilder.instances) == 2
    assert_disposed(StubBuilder.instances[0])
    replacement = registry_module.registry.require("example", workspace=str(tmp_path))
    assert replacement.builder is StubBuilder.instances[1]
    assert replacement.builder.actions == []


@pytest.mark.parametrize("missing_graph", [False, True])
def test_failed_attachment_never_disposes_preexisting_shared_handle(
    registry_module, mcp_modules, tmp_path, missing_graph,
):
    _, server = mcp_modules
    shared = create_handle(registry_module, tmp_path)
    shared.is_running = True
    shared.session_id = "shared-session"
    shared.completed_steps = ["Entry"]
    state = {"session_id": shared.session_id, "response": {"awaitingInput": "Gate"}}
    shared._mcp_run_state = state
    if not missing_graph:
        write_graph(tmp_path, content="invalid json")
    with pytest.raises((KeyError, json.JSONDecodeError)):
        server._attach_existing_workflow("example", workspace=str(tmp_path))
    assert registry_module.registry.get("example", workspace=str(tmp_path)) is shared
    assert len(StubBuilder.instances) == 1
    assert shared.builder.actions == []
    assert shared.is_running is True
    assert shared.session_id == "shared-session"
    assert shared.completed_steps == ["Entry"]
    assert shared._mcp_run_state is state


@pytest.mark.parametrize("reset_session", [False, True])
def test_builder_replacement_disposes_old_and_preserves_restart_semantics(
    registry_module, mcp_modules, tmp_path, reset_session,
):
    _, server = mcp_modules
    write_graph(tmp_path)
    old = create_handle(registry_module, tmp_path)
    old.is_running = True
    old.session_id = "old-session"
    old.completed_steps = ["Entry"]
    old.skills_root = "/stub/skills"
    old._mcp_run_state = {"session_id": old.session_id, "response": {"awaitingInput": "Gate"}}
    replacement, was_running = server._restart_builder_handle(
        "example", workspace=str(tmp_path), reset_session=reset_session,
    )
    assert was_running is True
    assert replacement is not old
    assert registry_module.registry.get("example", workspace=str(tmp_path)) is replacement
    assert_disposed(old.builder)
    old.close()
    assert_disposed(old.builder)
    assert replacement.builder.actions == []
    assert replacement.backend_port == 8765
    assert replacement.skills_root == "/stub/skills"
    assert replacement.builder.config["skills_root_path"] == "/stub/skills"
    if reset_session:
        assert replacement.session_id != "old-session"
    else:
        assert replacement.session_id == "old-session"
    assert replacement.completed_steps == []
    assert replacement._mcp_run_state is None


@pytest.mark.parametrize("failure", ["invalid_graph", "missing_graph", "constructor"])
def test_failed_builder_replacement_leaves_no_leaked_handles(
    registry_module, mcp_modules, tmp_path, monkeypatch, failure,
):
    _, server = mcp_modules
    old = create_handle(registry_module, tmp_path)
    old.is_running = True
    if failure != "missing_graph":
        write_graph(tmp_path, content="invalid json")
    if failure == "constructor":
        def fail_constructor(**_kwargs):
            raise RuntimeError("builder construction failed")
        monkeypatch.setattr(registry_module, "AgentBuilder", fail_constructor)
    with pytest.raises((KeyError, json.JSONDecodeError, RuntimeError)):
        server._restart_builder_handle("example", workspace=str(tmp_path))
    assert registry_module.registry.list_workflows() == []
    assert_disposed(old.builder)
    expected_builders = 2 if failure == "invalid_graph" else 1
    assert len(StubBuilder.instances) == expected_builders
    for builder in StubBuilder.instances:
        assert_disposed(builder)


def test_old_builder_cleanup_failure_aborts_replacement_without_leaking(
    registry_module, mcp_modules, tmp_path,
):
    _, server = mcp_modules
    write_graph(tmp_path)
    old = create_handle(registry_module, tmp_path)
    old.builder.pools[0].fail = True
    with pytest.raises(RuntimeError, match="pool cleanup failed"):
        server._restart_builder_handle("example", workspace=str(tmp_path))
    assert registry_module.registry.list_workflows() == []
    assert len(StubBuilder.instances) == 1
    assert_disposed(old.builder)
    old.close()
    assert_disposed(old.builder)


def test_restart_health_failure_still_disposes_transferred_handle(
    registry_module, mcp_modules, tmp_path, monkeypatch,
):
    _, server = mcp_modules
    old = create_handle(registry_module, tmp_path)
    old.is_running = True

    def fail_health(*_args, **_kwargs):
        raise RuntimeError("health probe failed")

    monkeypatch.setattr(server, "health_check", fail_health)
    with pytest.raises(RuntimeError, match="health probe failed"):
        server._restart_builder_handle("example", workspace=str(tmp_path))
    assert registry_module.registry.list_workflows() == []
    assert_disposed(old.builder)


@pytest.mark.parametrize("reset_session", [False, True])
def test_normal_stop_and_reload_retain_builder_and_clear_progress(
    registry_module, mcp_modules, tmp_path, reset_session,
):
    _, server = mcp_modules
    write_graph(tmp_path)
    handle = server._attach_existing_workflow("example", workspace=str(tmp_path))
    handle.backend_port = 8765
    handle.is_running = True
    handle.backend_process = StubProcess()
    handle.session_id = "old-session"
    handle.completed_steps = ["Entry"]
    handle._mcp_run_state = {"session_id": handle.session_id, "response": {"awaitingInput": "Gate"}}
    handle.stop_backend()
    builder = handle.builder
    result = json.loads(server.create_server().tools["reload_workflow"](
        "example", workspace=str(tmp_path), reset_session=reset_session,
    ))
    assert result["ok"] is True
    assert handle.builder is builder
    assert registry_module.registry.get("example", workspace=str(tmp_path)) is handle
    assert builder.close_calls == 0
    assert [pool.close_calls for pool in builder.pools] == [0, 0, 0, 0]
    assert builder.actions == ["stop", "stop", "stop"]
    if reset_session:
        assert handle.session_id != "old-session"
    else:
        assert handle.session_id == "old-session"
    assert handle.completed_steps == []
    assert handle._mcp_run_state is None
