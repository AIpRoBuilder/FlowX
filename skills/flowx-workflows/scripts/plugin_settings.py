# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Added 2026-11-10: read the python, workspace and API key saved by the DeepSeek Harness plugin.
"""Use the interpreter, workspace and API key saved in the FlowX Harness plugin settings.

Run directly to print the saved python and workspace as JSON (never the key).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_REEXEC_GUARD = "FLOWX_PLUGIN_PYTHON_ACTIVE"


def settings_path() -> Path:
    home = os.environ.get("DSH_HOME")
    base = Path(home) if home else Path.home() / ".dsh"
    return base / "plugins" / "deepseek-harness-flowx" / "settings.json"


def load() -> dict[str, Any]:
    """Return saved settings, or {} when absent or not an owner-only regular file."""
    path = settings_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 16384:
            return {}
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply(workspace: str | None) -> str | None:
    """Re-exec under the saved Python, export the saved API key and return the workspace to use."""
    saved = load()
    python = saved.get("python")
    if (isinstance(python, str) and os.path.isabs(python) and os.access(python, os.X_OK)
            and os.path.normpath(python) != os.path.normpath(sys.executable)
            and not os.environ.get(_REEXEC_GUARD)):
        os.environ[_REEXEC_GUARD] = "1"
        os.execv(python, [python, *sys.argv])
    key = saved.get("apiKey")
    if isinstance(key, str) and key:
        os.environ["FLOWX_LLM_API_KEY"] = key
    saved_workspace = saved.get("workspace")
    if workspace is None and isinstance(saved_workspace, str) and os.path.isabs(saved_workspace):
        return saved_workspace
    return workspace


if __name__ == "__main__":
    data = load()
    print(json.dumps({"python": data.get("python"), "workspace": data.get("workspace"),
                      "api_key_set": bool(data.get("apiKey"))}))
