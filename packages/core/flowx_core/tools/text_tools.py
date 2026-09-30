from __future__ import annotations


def truncate_context(
	text: str,
	*,
	label: str,
	max_chars: int,
	request_label: str = "request",
) -> str:
	if max_chars <= 0 or len(text) <= max_chars:
		return text

	head_chars = max_chars // 2
	tail_chars = max_chars - head_chars
	omitted = len(text) - max_chars
	return (
		f"{text[:head_chars]}\n\n"
		f"[truncated {label}: omitted {omitted} characters to keep the {request_label} bounded]\n\n"
		f"{text[-tail_chars:]}"
	)
