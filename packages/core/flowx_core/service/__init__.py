# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-27: lazy-export ServiceManager, RunJournal, and media so runtime modules stay lightweight.
"""Run and observe built workflow services."""

from __future__ import annotations

from importlib import import_module

__all__ = ["RunJournal", "ServiceManager", "manager", "media", "run_journal"]

_ATTR_MODULES = {
    "RunJournal": (".run_journal", "RunJournal"),
    "ServiceManager": (".manager", "ServiceManager"),
}
_SUBMODULES = frozenset({"manager", "media", "run_journal"})


def __getattr__(name: str):
    if name in _ATTR_MODULES:
        mod_name, attr = _ATTR_MODULES[name]
        value = getattr(import_module(mod_name, __name__), attr)
        globals()[name] = value
        return value
    if name in _SUBMODULES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
