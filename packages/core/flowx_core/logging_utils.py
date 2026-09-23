from __future__ import annotations

import logging
import sys
from pathlib import Path


RUNTIME_LOGGER_NAME = "flowx_core"
DEFAULT_RUNTIME_LOG_FILENAME = "flowx_core_debug.log"


def _normalize_log_level(level: str | int | None, default: int = logging.INFO) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelName(level.strip().upper())
        if isinstance(resolved, int):
            return resolved
    return default


def configure_runtime_logging(
    root_dir: str | Path,
    *,
    log_filename: str = DEFAULT_RUNTIME_LOG_FILENAME,
    console_level: str | int | None = None,
    file_level: str | int | None = None,
) -> Path:
    logger = logging.getLogger(RUNTIME_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    resolved_root = Path(root_dir).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    log_path = resolved_root / log_filename

    for handler in list(logger.handlers):
        if getattr(handler, "_flowx_core_runtime_file_handler", False):
            logger.removeHandler(handler)
            handler.close()

    resolved_console_level = _normalize_log_level(console_level, default=logging.INFO)
    resolved_file_level = _normalize_log_level(file_level, default=logging.DEBUG)

    console_handler = next(
        (
            handler
            for handler in logger.handlers
            if getattr(handler, "_flowx_core_runtime_console_handler", False)
        ),
        None,
    )
    if console_handler is None:
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler._flowx_core_runtime_console_handler = True
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)
    console_handler.setLevel(resolved_console_level)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler._flowx_core_runtime_file_handler = True
    file_handler.setLevel(resolved_file_level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(file_handler)
    return log_path


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or RUNTIME_LOGGER_NAME)