# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-10-15: clarify builder-owned artifacts and reusable workflows.
# Modified 2026-10-15: document deferred batch orchestration and partial-update risks.
# Modified 2026-10-16: expose the AgentBuilder engine API (build_*, run_service) through one builder.
# Modified 2026-10-19: add evolve (workflow, one node, or judged) and list_nodes.
# Modified 2026-10-20: expose per-node user-guided repair through build_nodes/build_workflow/evolve.
# Modified 2026-11-01: repair gating is the default; answers can target the generated test.
# Modified 2026-11-03: expose workflow allocation and building an allocated workflow.
# Modified 2026-11-03: expose graph-only builds for allocated workflows.
# Modified 2026-11-11: allow an explicit workflow folder name during allocation.
# Modified 2026-11-22: expose the contract protocol of the two FlowX entities (workflow builder and service
#   manager) instead of LLM-driven build methods; read methods keep their names and shapes.
"""Python SDK: drive the FlowX workflow builder as a coding agent, and run what it builds."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping

from flowx_core.agent_builder import AgentBuilder
from flowx_core.builder import BuildSettings, WorkflowBuilder
from flowx_core.service import ServiceManager


class FlowXClient:
    """One workspace's workflow builder and service manager behind a single object.

    FlowX writes no code itself. :meth:`begin_build` opens a build and returns *tasks*: contract
    packets that tell a coding agent what to produce (requirement, graph, one node). The agent
    answers each with :meth:`submit_task`; FlowX audits and tests the artifacts, publishes
    versions and renders the service entrypoint. :meth:`get_build_state` and
    :meth:`get_build_events` show where the build is. Workflows live under
    ``<workspace>/workflows/<workflow_id>``.
    """

    def __init__(self, *, workspace: str | Path | None = None, settings: BuildSettings | None = None) -> None:
        configured_workspace = workspace or os.environ.get("FLOWX_DEFAULT_WORKSPACE")
        self.workspace = Path(configured_workspace or ".flowx_workspaces").expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._settings = settings
        self._agent: AgentBuilder | None = None
        self._lock = RLock()

    def __enter__(self) -> FlowXClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def agent(self) -> AgentBuilder:
        with self._lock:
            if self._agent is None:
                self._agent = AgentBuilder(self.workspace, settings=self._settings)
            return self._agent

    @property
    def builder(self) -> WorkflowBuilder:
        return self.agent.builder

    @property
    def services(self) -> ServiceManager:
        return self.agent.services

    # -- workflows --------------------------------------------------------------------------
    def create_workflow(self, name: str | None = None, brief: str = "") -> str:
        """Allocate an empty workflow, optionally using ``name`` as its folder and ID."""
        return self.builder.create_workflow(name, brief)["workflow_id"]

    def workflows(self) -> dict[str, dict[str, Any]]:
        """Ids, versions and unresolved errors of every workflow in the workspace."""
        return self.builder.catalog()

    def list_nodes(self, workflow_id: str) -> list[dict[str, Any]]:
        """The workflow's nodes as ``{"step_id", "node_id", "version"}``; ``node_id`` is None until built."""
        return self.builder.list_nodes(workflow_id)

    def artifacts(self, workflow_id: str) -> dict[str, Any]:
        """Locations of a workflow's requirement, workflow.json, nodes and main entrypoint."""
        return self.builder.artifacts(workflow_id)

    # -- build protocol ---------------------------------------------------------------------
    def begin_build(self, workflow_id: str, intent: str = "", **options: Any) -> dict[str, Any]:
        """Open a build revision: the state plus ``next_tasks``; see :meth:`WorkflowBuilder.begin_build`."""
        return self.builder.begin_build(workflow_id, intent, **options)

    def get_build_state(self, workflow_id: str) -> dict[str, Any]:
        return self.builder.get_build_state(workflow_id)

    def get_tasks(self, workflow_id: str, *, compact: bool = False) -> list[dict[str, Any]]:
        return self.builder.get_tasks(workflow_id, compact=compact)

    def submit_task(self, workflow_id: str, task_id: str, artifacts: Mapping[str, Any]) -> dict[str, Any]:
        return self.builder.submit_task(workflow_id, task_id, artifacts)

    def get_build_events(self, workflow_id: str, since: int = 0, limit: int = 200) -> dict[str, Any]:
        return self.builder.get_build_events(workflow_id, since, limit)

    def reopen_stage(self, workflow_id: str, stage: str, step_id: str | None = None, reason: str = "",
                     **options: Any) -> dict[str, Any]:
        return self.builder.reopen_stage(workflow_id, stage, step_id, reason, **options)

    def abort_build(self, workflow_id: str, reason: str = "") -> dict[str, Any]:
        return self.builder.abort_build(workflow_id, reason)

    def build_recovery_state(self, workflow_id: str) -> dict[str, Any]:
        """``input_required`` while open tasks wait for the agent, otherwise ``clear``."""
        return self.builder.build_recovery_state(workflow_id)

    def build(
        self,
        workflow_id: str,
        intent: str,
        solve: Callable[[dict[str, Any]], Mapping[str, Any]],
        *,
        max_turns: int = 200,
        **options: Any,
    ) -> dict[str, Any]:
        """Run a build to its end, answering every task with ``solve(task) -> artifacts``.

        Returns the final build state; check its ``status`` (``completed`` or ``failed``).
        """
        self.begin_build(workflow_id, intent, **options)
        for _ in range(max_turns):
            tasks = self.get_tasks(workflow_id)
            if not tasks:
                return self.get_build_state(workflow_id)
            task = tasks[0]
            self.submit_task(workflow_id, task["task_id"], solve(task))
        raise RuntimeError(f"build of {workflow_id} did not finish within {max_turns} turns")

    # -- services ---------------------------------------------------------------------------
    def run_service(self, workflow_id: str, **options: Any) -> dict[str, Any]:
        """Start (or restart) the workflow's main entrypoint as a service; returns its status."""
        return self.services.run_service(workflow_id, **options)

    def stop_service(self, workflow_id: str) -> dict[str, Any]:
        return self.services.stop_service(workflow_id)

    def stop_all_services(self) -> None:
        """Stop managed services without waiting for anything else."""
        with self._lock:
            agent = self._agent
        if agent is not None:
            agent.services.stop_all()

    def service_status(self, workflow_id: str) -> dict[str, Any]:
        return self.services.status(workflow_id)

    def run_step(self, workflow_id: str, step_id: str, input: Any = None, **options: Any) -> dict[str, Any]:
        return self.services.run_step(workflow_id, step_id, input, **options)

    def reset_session(self, workflow_id: str, session_id: str = "") -> dict[str, Any]:
        return self.services.reset_session(workflow_id, session_id)

    def list_runs(self, workflow_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.services.list_runs(workflow_id, limit)

    def get_run(self, workflow_id: str, run_id: str = "latest") -> dict[str, Any]:
        return self.services.get_run(workflow_id, run_id)

    def evidence(self, workflow_id: str, run_id: str = "latest") -> dict[str, Any]:
        """Run journal and service log tail to pass as ``runtime_evidence`` to :meth:`begin_build`."""
        return self.services.evidence(workflow_id, run_id)

    def close(self) -> None:
        """Stop the services started here and abort open builds; safe to call repeatedly."""
        with self._lock:
            agent, self._agent = self._agent, None
        if agent is not None:
            agent.close()
