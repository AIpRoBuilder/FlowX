# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-10-13: reconcile input metadata with generated node constants without importing them.
# Modified 2026-10-13: serialize cached pool ownership during creation and cleanup.
# Modified 2026-10-14: run main generation and auditing as a single-pass workflow without repair retries.
# Modified 2026-10-14: delegate main-entrypoint requests to MainEntrypointGenerationWorkflow.
# Modified 2026-10-14: inject service state, components, and callbacks independently of AgentBuilder.
# Modified 2026-10-14: delegate graph building to a skippable single-pass workflow.
# Modified 2026-10-14: own one initialized workflow per service; accept all run state in requests.
# Modified 2026-10-15: repair results use the unified request model.
# Modified 2026-10-16: remove the workflow service wrappers; AgentBuilder owns those workflows directly.
# Modified 2026-11-03: isolate service subprocess output from MCP stdio.
# Modified 2026-11-16: apply the workflow service folder's .env when starting or smoke-testing it.
# Modified 2026-11-22: keep only the service process environment; ServiceManager owns running services and the
#   builder no longer derives step metadata.
"""Process environment for generated workflow services."""

from __future__ import annotations

import os
from pathlib import Path

try:  # Declared runtime dependency; optional import keeps unrelated callers importable.
    from dotenv import dotenv_values
except Exception:  # pragma: no cover - exercised only in broken installations
    dotenv_values = None  # type: ignore[assignment]


def service_environment(main_path: Path) -> dict[str, str]:
    """Process environment plus the workflow service folder's .env, which wins on conflicts."""
    environment = dict(os.environ)
    dotenv_path = main_path.parent / ".env"
    if dotenv_values is None or not dotenv_path.is_file():
        return environment
    try:
        values = dotenv_values(dotenv_path)
    except Exception:
        return environment
    environment.update({
        key: value for key, value in values.items()
        if isinstance(key, str) and isinstance(value, str)
    })
    return environment


__all__ = ["service_environment"]
