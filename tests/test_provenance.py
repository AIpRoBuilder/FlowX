from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load_module("flowx_provenance_checker", "tools/provenance.py")


def test_public_origin_matches_packaged_metadata() -> None:
    metadata = load_module("flowx_origin_metadata", "packages/core/flowx_core/provenance.py")
    assert metadata.ORIGIN_ID == checker.ORIGIN_ID
    assert metadata.UPSTREAM_REPOSITORY == checker.UPSTREAM
    assert metadata.LICENSE_IDENTIFIER == "Apache-2.0"


def test_published_snapshot_records_valid_fingerprints() -> None:
    manifest = json.loads(checker.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["origin_id"] == checker.ORIGIN_ID
    assert set(manifest["files"]) == set(checker.SOURCE_FILES)
    for record in manifest["files"].values():
        assert len(record["sha256"]) == 64
        assert all(character in "0123456789abcdef" for character in record["sha256"])


def test_fresh_snapshot_matches_source_tree() -> None:
    assert checker.compare(ROOT, checker.snapshot(ROOT))["all_exact"]


def test_ast_comparison_ignores_only_formatting_and_comments(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 42\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "origin_id": checker.ORIGIN_ID,
        "python_ast_version": checker.snapshot(ROOT)["python_ast_version"],
        "files": {"example.py": checker.fingerprints(source)},
    }
    assert checker.compare(tmp_path, manifest)["all_exact"]
    source.write_text("# removed attribution or added a comment\nvalue=42\n", encoding="utf-8")
    report = checker.compare(tmp_path, manifest)
    assert not report["all_exact"]
    assert report["files"][0]["status"] == "python_ast_match"
    source.write_text("value = 43\n", encoding="utf-8")
    assert checker.compare(tmp_path, manifest)["files"][0]["status"] == "changed"
    source.unlink()
    assert checker.compare(tmp_path, manifest)["files"][0]["status"] == "missing"


def test_no_candidate_code_is_executed(tmp_path: Path) -> None:
    source = tmp_path / "candidate.py"
    source.write_text("raise RuntimeError('never execute this candidate')\n", encoding="utf-8")
    assert "python_ast_sha256" in checker.fingerprints(source)


def test_invalid_python_keeps_raw_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "candidate.py"
    source.write_text("def broken(\n", encoding="utf-8")
    assert set(checker.fingerprints(source)) == {"sha256"}


@pytest.mark.parametrize("relative", ["../outside.py", "/etc/passwd", "a/../../outside", "a\\outside", ""])
def test_unsafe_manifest_paths_are_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        checker.contained_file(tmp_path, relative)


def test_symlinks_cannot_escape_target_tree(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    (root / "escape.py").symlink_to(outside)
    with pytest.raises(ValueError):
        checker.contained_file(root, "escape.py")


def test_ast_match_is_disabled_on_another_python_version(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "origin_id": checker.ORIGIN_ID,
        "python_ast_version": "0.0",
        "files": {"example.py": checker.fingerprints(source)},
    }
    source.write_text("# different bytes\nvalue = 1\n", encoding="utf-8")
    report = checker.compare(tmp_path, manifest)
    assert not report["python_ast_comparison_enabled"]
    assert report["files"][0]["status"] == "changed"


@pytest.mark.parametrize("invalid", [{}, []])
def test_cli_returns_input_error_for_invalid_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str], invalid: object) -> None:
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(invalid), encoding="utf-8")
    assert checker.main(["verify", "--manifest", str(manifest)]) == 2
    assert "Provenance check failed" in capsys.readouterr().err


def test_cli_reports_exact_and_changed_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "copy"
    root.mkdir()
    source = root / "example.py"
    source.write_text("value = 42\n", encoding="utf-8")
    manifest = tmp_path / "trusted.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "origin_id": checker.ORIGIN_ID,
        "files": {"example.py": checker.fingerprints(source)},
    }), encoding="utf-8")
    arguments = ["verify", "--root", str(root), "--manifest", str(manifest)]
    assert checker.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["all_exact"]
    source.write_text("value = 43\n", encoding="utf-8")
    assert checker.main(arguments) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["files"][0]["expected_sha256"] != report["files"][0]["actual_sha256"]