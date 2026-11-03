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
"""Python SDK for building, modifying and running FlowX workflows."""

from __future__ import annotations

import os
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Optional

from flowx_core.agent_builder import AgentBuilder
from flowx_core.agent_workflow_engine import AgentResult


class FlowXClient:
    """Build and run workflows through one :class:`AgentBuilder`.

    Every build method returns an :class:`AgentResult`; stage failures are reported in it
    (``ok`` is false, ``errors`` says why), not raised. Call ``result.raise_for_failure()``
    to turn them into an exception. Workflows are identified by the ``workflow_id`` of the
    result that created them and live under ``<workspace>/workflows/<workflow_id>``.

    The builder publishes its generation workflows through module-level state, so use one
    client per process. Extra keyword ``options`` are passed on to the engine's request
    (``temperature``, ``max_retries``, ``cascade``, ``fastapi_port``, ``node_options`` ...).
    """

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        configured_workspace = workspace or os.environ.get("FLOWX_DEFAULT_WORKSPACE")
        self.workspace = Path(configured_workspace or ".flowx_workspaces").expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.environ.get("FLOWX_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self.model = model or os.environ.get("FLOWX_LLM_MODEL", "deepseek-flash")
        self.provider = provider or os.environ.get("FLOWX_LLM_PROVIDER", "deepseek")
        self._builder: AgentBuilder | None = None
        self._lock = RLock()

    def __enter__(self) -> FlowXClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def builder(self) -> AgentBuilder:
        """The builder, created on first use because it allocates native workflow pools."""
        with self._lock:
            if self._builder is None:
                self._builder = AgentBuilder(
                    api_key=self.api_key,
                    model=self.model,
                    provider=self.provider,
                    root_dir=str(self.workspace),
                )
            return self._builder

    def build_workflow_json(self, prompt: str, *, workflow_id: str | None = None, **options: Any) -> AgentResult:
        """Create (no ``workflow_id``) or modify workflow.json together with its requirement analysis."""
        text = str(prompt).strip()
        if not text:
            raise ValueError("prompt must be a non-empty string")
        return self.builder.build_workflow_json(text, workflow_id=workflow_id, **options)

    def build_nodes(
        self,
        workflow_id: str,
        amendments: Mapping[str, str] | None = None,
        **options: Any,
    ) -> AgentResult:
        """Build the nodes of a workflow, or amend specific nodes by step id (``{step_id: amendment}``).

        A failing node test pauses at its own gate by default: the result has
        ``status="input_required"`` with ``request_id``/``awaiting`` and is resumed with
        :meth:`answer`. Answers amend the node, while ``test: <guidance>`` fixes the
        generated test instead. Pass ``await_repair_input=False`` to amend automatically.
        """
        return self.builder.build_nodes(workflow_id, amendments, **options)

    def build_main_entrypoint(self, workflow_id: str, amendment: str = "", **options: Any) -> AgentResult:
        """Create or regenerate the main entrypoint from the workflow's latest nodes."""
        return self.builder.build_main_entrypoint(workflow_id, amendment, **options)

    def build_workflow(
        self,
        prompt: str = "",
        *,
        workflow_id: str | None = None,
        **options: Any,
    ) -> AgentResult:
        """Run the whole engine on a free-text request and/or ``logs=[...]``; later stages follow earlier changes."""
        return self.builder.build_workflow(prompt, workflow_id=workflow_id, **options)

    def create_workflow(self) -> str:
        """Allocate an empty workflow and return its identifier before building it."""
        return self.builder.engine.resolve_record(None).workflow_id

    def build_new_workflow(self, workflow_id: str, prompt: str, **options: Any) -> AgentResult:
        """Build a workflow previously allocated by :meth:`create_workflow`."""
        return self.builder.engine.build_new_workflow(workflow_id, prompt, **options)

    def build_allocated_workflow_json(self, workflow_id: str, prompt: str, **options: Any) -> AgentResult:
        """Create or modify only an allocated workflow's graph and requirements."""
        return self.builder.engine.build_allocated_workflow_json(workflow_id, prompt, **options)

    def evolve(
        self,
        workflow_id: str,
        command: str,
        *,
        target: str | None = None,
        **options: Any,
    ) -> AgentResult:
        """Evolve an existing workflow by ``command``.

        ``target="workflow"`` changes the workflow itself (its graph; nodes and main entrypoint
        follow with ``cascade``). A node step id or node id from :meth:`list_nodes` changes only
        that node. Without ``target`` the engine judges which part the command refers to.
        """
        text = str(command).strip()
        if not text:
            raise ValueError("command must be a non-empty string")
        return self.builder.evolve(workflow_id, text, target=target, **options)

    def answer(self, request_id: str, input: str = "", step_id: str = "") -> AgentResult:
        """Resume a paused build with one node's guidance (``step_id`` targets one chain).

        ``input`` is combined with that node's pytest log and amends the node,
        ``test: <guidance>`` amends the generated test instead, ``"skip"`` skips the
        amendment, and an empty string amends the node from the log alone.
        """
        return self.builder.engine.answer(request_id, input, step_id)

    def abort(self, request_id: str) -> None:
        """Cancel a paused build and release its unpublished candidate revision."""
        self.builder.engine.abort(request_id)

    def pending_requests(self) -> list[dict[str, Any]]:
        """Paused builds as ``{"request_id", "workflow_id", "awaiting"}``."""
        return self.builder.engine.pending_requests()

    def build_recovery_state(self, workflow_id: str) -> dict[str, Any]:
        """Check both the agent pause and retained native candidate state."""
        return self.builder.engine.build_recovery_state(workflow_id)

    def list_nodes(self, workflow_id: str) -> list[dict[str, Any]]:
        """The workflow's nodes as ``{"step_id", "node_id", "version"}``; ``node_id`` is None until built."""
        return self.builder.engine.list_nodes(workflow_id)

    def run_service(self, workflow_id: str) -> dict[str, Any]:
        """Start (or restart) the workflow's main entrypoint as a service; returns process info."""
        return self.builder.run_service(workflow_id)

    def stop_service(self, workflow_id: str) -> None:
        self.builder.stop_service(workflow_id)

    def stop_all_services(self) -> None:
        """Stop managed services without waiting for a long-running build to finish."""
        with self._lock:
            builder = self._builder
        if builder is not None:
            builder.engine._stop_services()

    def workflows(self) -> dict[str, dict[str, Any]]:
        """Ids, versions and unresolved errors of every workflow in the workspace."""
        return self.builder.engine.catalog()

    def artifacts(self, workflow_id: str) -> dict[str, Any]:
        """Locations of a workflow's requirement, workflow.json, nodes and main entrypoint."""
        return self.builder.engine.artifacts(workflow_id)

    def close(self) -> None:
        """Stop the services started here and release the builder; safe to call repeatedly."""
        with self._lock:
            builder, self._builder = self._builder, None
        if builder is not None:
            builder.close()


def result_to_dict(result: AgentResult) -> dict[str, Any]:
    """A plain-dict view of an :class:`AgentResult`; values may still hold ``Path`` objects."""
    return {
        "ok": result.ok,
        "workflow_id": result.workflow_id,
        "intent": result.intent.to_dict() if result.intent else None,
        "stages": {name: stage.to_dict() for name, stage in result.stages.items()},
        "errors": dict(result.errors),
        "logs": dict(result.logs),
        "artifacts": dict(result.artifacts),
        "service": dict(result.service) if result.service else None,
        "status": result.status,
        "request_id": result.request_id,
        "awaiting": dict(result.awaiting) if result.awaiting else None,
    }
