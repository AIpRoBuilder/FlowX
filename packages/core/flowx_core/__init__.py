from importlib import import_module


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
