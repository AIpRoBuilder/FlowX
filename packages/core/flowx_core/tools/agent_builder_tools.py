from __future__ import annotations

import shutil


def select_python_command() -> str:
    for candidate in ("python3.10", "python3", "python"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "python"
