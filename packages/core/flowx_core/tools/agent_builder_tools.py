from __future__ import annotations

import shutil
from pathlib import Path


def get_language_extension(language: str) -> str:
    language_clean = language.strip().lower() if language else "python"
    if language_clean in {"python", "py"}:
        return ".py"
    if language_clean in {"javascript", "js"}:
        return ".js"
    if language_clean in {"typescript", "ts"}:
        return ".ts"
    return f".{language_clean}"


def select_python_command() -> str:
    for candidate in ("python3.10", "python3", "python"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "python"
