# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
from importlib import import_module

from .provenance import ORIGIN_ID as __origin_id__
from .provenance import UPSTREAM_REPOSITORY as __upstream_repository__

__version__ = "0.2.0"

__all__ = [
    "architect",
    "auditor",
    "context_builder",
    "demand_analyzer",
    "llm_client",
    "tools",
    "worker",
]


def __getattr__(name: str):
    if name in __all__:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
