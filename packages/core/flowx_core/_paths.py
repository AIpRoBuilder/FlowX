from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def resolve_package_root(current_file: str) -> Path:
    default_package_root = Path(current_file).resolve().parents[1]
    core_spec = importlib.util.find_spec("core")
    if core_spec and core_spec.origin:
        return Path(core_spec.origin).resolve().parent
    return default_package_root


def ensure_package_parent_on_sys_path(root_dir: Path) -> Path:
    parent_dir = str(root_dir.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    return root_dir


def bootstrap_package_root(current_file: str) -> Path:
    return ensure_package_parent_on_sys_path(resolve_package_root(current_file))
