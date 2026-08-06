from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

load_dotenv = None
try:
    load_dotenv = importlib.import_module("dotenv").load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None


def _load_env(root: Path) -> None:
    if load_dotenv is None:
        return
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)


def _inject_path(path_value: Path) -> None:
    resolved = path_value.expanduser().resolve()
    if not resolved.exists():
        return
    resolved_str = str(resolved)
    if resolved_str not in sys.path:
        sys.path.insert(0, resolved_str)


def _bootstrap_paths(root: Path) -> None:
    extra_paths = os.environ.get("FLOWX_EXTRA_PATHS", "")
    if extra_paths.strip():
        for raw in extra_paths.split(os.pathsep):
            candidate = raw.strip()
            if candidate:
                _inject_path(Path(candidate))

    for candidate in (
        root.parent / "meta_agent",
        root.parent / "ag_ui_worflow",
    ):
        _inject_path(candidate)


def main() -> None:
    root = Path(__file__).resolve().parent
    _load_env(root)
    _bootstrap_paths(root)

    run_main = importlib.import_module("flowx_mcp.server").main

    run_main()


if __name__ == "__main__":
    main()