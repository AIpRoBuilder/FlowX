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


def _inject_path(path_value: Path) -> None:
    resolved = path_value.expanduser().resolve()
    if not resolved.exists():
        return
    resolved_str = str(resolved)
    if resolved_str not in sys.path:
        sys.path.insert(0, resolved_str)


def _candidate_roots(module_file: str) -> list[Path]:
    package_root = Path(module_file).resolve().parent.parent
    roots: list[Path] = []
    seen: set[str] = set()

    for candidate in (
        os.environ.get("FLOWX_CONFIG_ROOT", "").strip(),
        str(Path.cwd()),
        str(package_root),
    ):
        if not candidate:
            continue
        resolved = str(Path(candidate).expanduser().resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(Path(resolved))
    return roots


def load_env_from_runtime_context(module_file: str) -> None:
    if load_dotenv is None:
        return
    for root in _candidate_roots(module_file):
        env_path = root / ".env"
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)


def bootstrap_import_paths(module_file: str) -> None:
    extra_paths = os.environ.get("FLOWX_EXTRA_PATHS", "")
    if extra_paths.strip():
        for raw in extra_paths.split(os.pathsep):
            candidate = raw.strip()
            if candidate:
                _inject_path(Path(candidate))

    for root in _candidate_roots(module_file):
        for candidate in (
            root.parent / "meta_agent",
            root.parent / "ag_ui_workflow",
            root.parent / "ag_ui_worflow",
        ):
            _inject_path(candidate)