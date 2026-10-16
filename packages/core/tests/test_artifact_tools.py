from __future__ import annotations

import hashlib
import json
from pathlib import Path

from flowx_core.tools.artifact_tools import (
	fingerprint_json,
	json_text,
	make_test_relocatable,
	relocate_python_path_literals,
	render_json_context,
	text_digest,
)


def test_json_text_and_fingerprint_keep_canonical_workflow_format() -> None:
	value = {"z": 1, "a": "雪"}
	serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)

	assert json_text(value) == serialized
	assert fingerprint_json(value) == hashlib.sha256(serialized.encode("utf-8")).hexdigest()
	assert text_digest("payload") == hashlib.sha256(b"payload").hexdigest()


def test_render_json_context_orders_keys_and_accepts_custom_heading() -> None:
	context = render_json_context({"z": {"value": 2}, "a": {"value": 1}}, heading="Inputs:")

	assert context == f"Inputs:\n{json_text({'value': 1})}\n{json_text({'value': 2})}"


def test_make_test_relocatable_preserves_comments_and_rewrites_only_root_paths(tmp_path: Path) -> None:
	root = tmp_path / "candidate"
	test_file = tmp_path / "bundle" / "tests" / "test_node.py"
	test_file.parent.mkdir(parents=True)
	root.mkdir()
	test_file.write_text(
		f"# retained notice\nNODE = {str(root / 'Node.py')!r}\nOTHER = '/outside/Node.py'\n",
		encoding="utf-8",
	)

	count = make_test_relocatable(test_file, root)
	updated = test_file.read_text(encoding="utf-8")
	compile(updated, str(test_file), "exec")
	assert count == 1
	assert "# retained notice" in updated
	assert "Path(__file__).resolve().parents[1] / 'Node.py'" in updated
	assert "OTHER = '/outside/Node.py'" in updated


def test_relocate_python_path_literals_supports_custom_bundle_expression(tmp_path: Path) -> None:
	root = tmp_path / "source"
	root.mkdir()
	test_file = tmp_path / "test_paths.py"
	test_file.write_text(f"TARGET = {str(root / 'nested' / 'item.json')!r}\n", encoding="utf-8")

	assert relocate_python_path_literals(test_file, root, base_expression="ROOT") == 1
	assert "str(ROOT / 'nested/item.json')" in test_file.read_text(encoding="utf-8")