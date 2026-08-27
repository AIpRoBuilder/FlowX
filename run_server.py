from __future__ import annotations

import importlib
from flowx_mcp.bootstrap import bootstrap_import_paths, load_env_from_runtime_context

load_env_from_runtime_context(__file__)
bootstrap_import_paths(__file__)


def main() -> None:
    run_main = importlib.import_module("flowx_mcp.server").main

    run_main()


if __name__ == "__main__":
    main()