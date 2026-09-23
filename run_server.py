from __future__ import annotations

import importlib
import sys
from pathlib import Path

_packages_dir = Path(__file__).resolve().parent / "packages"
for _component in ("a2a", "sdk", "mcp", "core"):
    _component_root = str(_packages_dir / _component)
    if _component_root not in sys.path:
        sys.path.insert(0, _component_root)

from flowx_mcp.bootstrap import bootstrap_import_paths, load_env_from_runtime_context

load_env_from_runtime_context(__file__)
bootstrap_import_paths(__file__)


def main() -> None:
    run_main = importlib.import_module("flowx_mcp.server").main

    run_main()


if __name__ == "__main__":
    main()