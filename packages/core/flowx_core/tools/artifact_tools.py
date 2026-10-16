# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
"""Reusable serialization, digest, context-rendering, and artifact helpers."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import tokenize
from pathlib import Path
from typing import Any, Mapping

from flowx_core.tools.file_tools import (
	collect_session_state_keys_from_node_file,
	compile_node_file_and_get_derived_keys,
	compile_node_file_and_get_step_output_card_schema,
)


def json_text(
	value: Any,
	*,
	ensure_ascii: bool = False,
	sort_keys: bool = True,
	indent: int | str | None = 2,
	allow_nan: bool = False,
	**options: Any,
) -> str:
	"""Serialize JSON with stable, readable defaults and optional ``json.dumps`` settings."""
	return json.dumps(
		value,
		ensure_ascii=ensure_ascii,
		sort_keys=sort_keys,
		indent=indent,
		allow_nan=allow_nan,
		**options,
	)


def text_digest(text: str, *, algorithm: str = "sha256", encoding: str = "utf-8") -> str:
	"""Return a hexadecimal digest of text using a configurable hash algorithm."""
	digest = hashlib.new(algorithm)
	digest.update(text.encode(encoding))
	return digest.hexdigest()


def fingerprint_json(value: Any) -> str:
	"""Hash canonical JSON; defaults preserve the dynamic workflow fingerprint format."""
	return text_digest(json_text(value))


def render_json_context(
	items: Mapping[str, Any],
	*,
	heading: str,
	sort_keys: bool = True,
	item_separator: str = "\n",
	json_options: Mapping[str, Any] | None = None,
) -> str:
	"""Render named JSON items under a heading, with deterministic ordering by default."""
	keys = sorted(items) if sort_keys else items
	sections = (json_text(items[key], **dict(json_options or {})) for key in keys)
	return f"{heading}\n" + item_separator.join(sections)


def relocate_python_path_literals(
	path: str | Path,
	source_root: str | Path,
	*,
	base_expression: str,
) -> int:
	"""Rewrite Python string literals beneath ``source_root`` as relative path expressions.

	The token-based rewrite preserves comments and formatting. ``base_expression``
	should be a Python expression resolving to the destination bundle root.
	Returns the number of rewritten literals.
	"""
	test_path = Path(path)
	root = Path(source_root).expanduser().resolve()
	root_text = str(root)
	source = test_path.read_text(encoding="utf-8")
	tokens = []
	rewritten = 0
	for token in tokenize.generate_tokens(io.StringIO(source).readline):
		if token.type == tokenize.STRING:
			try:
				value = ast.literal_eval(token.string)
			except (ValueError, SyntaxError):
				value = None
			if isinstance(value, str) and (value == root_text or value.startswith(root_text + os.sep)):
				relative = Path(value).relative_to(root).as_posix()
				expression = f"str({base_expression} / {relative!r})"
				token = token._replace(string=expression)
				rewritten += 1
		tokens.append(token)
	updated = tokenize.untokenize(tokens)
	compile(updated, str(test_path), "exec")
	test_path.write_text(updated, encoding="utf-8")
	return rewritten


def make_test_relocatable(
	path: str | Path,
	directory: str | Path,
	*,
	bundle_root_expression: str = "__import__('pathlib').Path(__file__).resolve().parents[1]",
) -> int:
	"""Make generated tests independent of an ephemeral workflow directory.

	Literal paths inside ``directory`` are made relative to the test bundle. Logs
	are intentionally not rewritten because they describe the original run.
	"""
	return relocate_python_path_literals(
		path,
		directory,
		base_expression=bundle_root_expression,
	)


def node_source_artifact(name: str, path: str | Path) -> dict[str, Any]:
	"""Build stable source and interface metadata for a generated workflow node."""
	source_path = Path(path)
	source = source_path.read_text(encoding="utf-8")
	return {
		"parent_node": name,
		"source_path": str(source_path),
		"source": source,
		"source_digest": text_digest(source),
		"derived_keys": compile_node_file_and_get_derived_keys(str(source_path)),
		"output_card_preview": compile_node_file_and_get_step_output_card_schema(str(source_path)),
		"session_state_keys": sorted(collect_session_state_keys_from_node_file(str(source_path), name)),
	}


__all__ = [
	"fingerprint_json",
	"json_text",
	"make_test_relocatable",
	"node_source_artifact",
	"relocate_python_path_literals",
	"render_json_context",
	"text_digest",
]