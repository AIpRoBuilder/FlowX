# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: cover service re-rendering on tampered service files, .env preservation, and snapshot integrity gating.
"""The service manager runs, drives and inspects what the builder produced."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from builder_fixtures import CHAIN, Agent, good, plan_script

from flowx_core.builder import WorkflowBuilder, WorkflowFilesModified
from flowx_core.service import ServiceManager


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def stack(tmp_path: Path):
    builder = WorkflowBuilder(storage_dir=tmp_path / "workflows")
    services = ServiceManager(builder, ready_timeout=30.0)
    yield builder, services
    services.close()
    builder.close()


def build(builder: WorkflowBuilder, **options) -> str:
    workflow_id = builder.create_workflow()["workflow_id"]
    script = {**plan_script(CHAIN), "node:Greeter": [good("Greeter")], "node:Shouter": [good("Shouter", ["Greeter"])]}
    builder.begin_build(workflow_id, "Greet and shout", fastapi_host="127.0.0.1", fastapi_port=free_port(), **options)
    Agent(builder, workflow_id, script).run()
    assert builder.get_build_state(workflow_id)["status"] == "completed"
    return workflow_id


def test_service_runs_steps_and_journals_results(stack) -> None:
    builder, services = stack
    workflow_id = build(builder)

    assert services.status(workflow_id)["running"] is False
    started = services.run_service(workflow_id)
    assert started["running"] is True and started["base_url"].startswith("http://127.0.0.1:")

    first = services.run_step(workflow_id, "Greeter", "")
    assert first["status"] == "completed", first
    assert services.get_run(workflow_id)["run_id"]
    assert services.list_runs(workflow_id)[0]["status"] in {"completed", "running"}
    evidence = services.evidence(workflow_id)
    assert evidence["run"]["run_id"] == services.get_run(workflow_id)["run_id"]
    assert evidence["errors"] == {} and "service_log_tail" in evidence

    assert services.reset_session(workflow_id)["session_id"] == first["session_id"]
    assert services.stop_service(workflow_id)["running"] is False
    with pytest.raises(RuntimeError, match="not running"):
        services.run_step(workflow_id, "Greeter", "")


def test_service_requires_a_built_entrypoint_and_safe_run_ids(stack) -> None:
    builder, services = stack
    workflow_id = builder.create_workflow()["workflow_id"]
    with pytest.raises(FileNotFoundError, match="no main entrypoint"):
        services.run_service(workflow_id)
    with pytest.raises(FileNotFoundError, match="no runs"):
        services.get_run(workflow_id)
    with pytest.raises(ValueError, match="plain run identifier"):
        services.get_run(workflow_id, "../agent")


def test_build_can_smoke_test_the_rendered_entrypoint(stack) -> None:
    builder, services = stack
    workflow_id = build(builder, smoke_test=True)

    assert builder.get_build_state(workflow_id)["summary"]["main_entrypoint_path"]
    assert (builder.record(workflow_id).main_log_path).is_file()
    assert services.status(workflow_id)["running"] is False


def test_smoke_test_needs_a_service_manager(tmp_path: Path) -> None:
    builder = WorkflowBuilder(storage_dir=tmp_path / "w")
    try:
        workflow_id = builder.create_workflow()["workflow_id"]
        with pytest.raises(ValueError, match="service manager"):
            builder.begin_build(workflow_id, "x", smoke_test=True)
    finally:
        builder.close()


def test_run_service_rerenders_tampered_service_package_and_preserves_env(stack) -> None:
    builder, services = stack
    workflow_id = build(builder)
    record = builder.record(workflow_id)
    env_file = record.service_dir / ".env"
    env_file.write_text("DEMO_KEY=secret\n", encoding="utf-8")
    assert builder.verify_workflow(workflow_id)["ok"] is True

    service_greeter = record.service_dir / "Greeter.py"
    original = service_greeter.read_text(encoding="utf-8")
    service_greeter.write_text(original + "\n# direct edit\n", encoding="utf-8")
    assert builder.verify_workflow(workflow_id)["ok"] is False

    started = services.run_service(workflow_id)
    assert started["running"] is True
    assert service_greeter.read_text(encoding="utf-8") == original
    assert env_file.read_text(encoding="utf-8") == "DEMO_KEY=secret\n"
    assert builder.verify_workflow(workflow_id)["ok"] is True
    services.stop_service(workflow_id)


def test_run_service_blocks_when_snapshot_is_tampered(stack) -> None:
    builder, services = stack
    workflow_id = build(builder)
    snapshot_greeter = Path(builder.artifacts(workflow_id)["nodes"]["Greeter"]["artifacts"]["source"])
    snapshot_greeter.write_text("# tampered snapshot\n", encoding="utf-8")

    with pytest.raises(WorkflowFilesModified):
        services.run_service(workflow_id)
