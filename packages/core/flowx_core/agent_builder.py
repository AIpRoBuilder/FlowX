# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified: explicitly release cached native workflow pools on reset/shutdown.
# Modified 2026-10-13: delegate pool cleanup to synchronized service cache owners.
# Modified 2026-10-14: wire explicit service dependencies and refresh components after LLM resets.
# Modified 2026-10-14: graph service delegates to a single-pass workflow without repair-loop injection.
# Modified 2026-10-14: own session bookkeeping and dispatch explicit requests to initialized services.
# Modified 2026-10-14: refresh request graph snapshots and merge repair publications under a builder-owned lock.
# Modified 2026-10-15: use unified workflow requests instead of separate context models.
# Modified 2026-10-15: keep routing local and remove redundant request identities/bookkeeping.
# Modified 2026-10-15: orchestrate batches locally and dispatch complete per-node requests without callback registries.
# Modified 2026-10-15: disable batch node generation and remove batch-only helpers; retain per-node workflows.
# Modified 2026-10-16: publish the node generation workflow and runtime modifier for DynamicWorkflowOrchestrator.
# Modified 2026-10-16: own the request-driven AgentWorkflowEngine and publish the workflows it reads.
# Modified 2026-10-16: remove the unused audit-round settings and node-test coder factory.
# Modified 2026-10-16: own the four generation workflows directly instead of through service wrappers.
# Modified 2026-10-16: keep only workflow.json/requirement, node, main entrypoint, engine and service builders.
# Modified 2026-10-28: remove the legacy single-pass builder API now owned by AgentWorkflowEngine.
# Modified 2026-11-22: reduce the builder to the two FlowX entities (WorkflowBuilder, ServiceManager); the host
#   coding agent writes the artifacts, so the LLM clients, coders and planners are gone.
"""The FlowX entry point: one workspace with a workflow builder and a service manager."""

from __future__ import annotations

from pathlib import Path

from flowx_core.builder import BuildSettings, WorkflowBuilder
from flowx_core.service import ServiceManager


class AgentBuilder:
    """Own a workspace's :class:`WorkflowBuilder` and the :class:`ServiceManager` that runs its output.

    Workflows live under ``<root_dir>/workflows/<workflow_id>``. ``builder`` hands contract packets
    to a coding agent and publishes what it submits; ``services`` runs, drives and inspects the
    generated services. No LLM is called: the agent that drives the builder writes the code.
    """

    def __init__(self, root_dir: str | Path = "./example", *, settings: BuildSettings | None = None) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.builder = WorkflowBuilder(storage_dir=self.root_dir / "workflows", settings=settings)
        self.services = ServiceManager(self.builder)

    def close(self) -> None:
        """Stop the services started here and abort open builds; safe to call repeatedly."""
        try:
            self.services.close()
        finally:
            self.builder.close()


__all__ = ["AgentBuilder"]
