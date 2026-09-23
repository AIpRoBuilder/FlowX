from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from flowx_core.logging_utils import get_logger


LOGGER = get_logger(__name__)


def _run_stage(stage_name: str, target_path: Path, writer: Callable[..., Any], **kwargs: Any) -> Any:
	"""Run a named write stage and print detailed failure context before re-raising."""
	LOGGER.info("start writing %s to: %s", stage_name, target_path)
	try:
		return writer(**kwargs)
	except Exception as exc:
		LOGGER.error(
			"failed writing %s to %s: %s: %s",
			stage_name,
			target_path,
			exc.__class__.__name__,
			exc,
			exc_info=True,
		)
		raise
