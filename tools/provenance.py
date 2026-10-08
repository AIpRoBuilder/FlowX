# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
"""Offline source fingerprints: no networking, tracking, or copy prevention."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ORIGIN_ID = "urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466"
UPSTREAM = "https://github.com/AIpRoBuilder/FlowX"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "provenance" / "source-manifest.json"
SOURCE_FILES = (
    "LICENSE",
    "NOTICE",
    "AGENTS.md",
    "pyproject.toml",
    "MANIFEST.in",
    "packages/core/flowx_core/__init__.py",
    "packages/core/flowx_core/provenance.py",
    "packages/core/flowx_core/agent_builder.py",
    "packages/core/flowx_core/builder_services.py",
    "packages/mcp/flowx_mcp/server.py",
    "packages/sdk/flowx_sdk/client.py",
    "packages/a2a/flowx_a2a/server.py",
    "tools/provenance.py",
)


def contained_file(root: Path, relative: str) -> Path:
    """Reject absolute paths, traversal, and symlinks escaping the target tree."""
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError(f"Unsafe source path: {relative!r}")
    target = (root / path).resolve()
    target.relative_to(root.resolve())
    return target


def fingerprints(path: Path) -> dict[str, str]:
    content = path.read_bytes()
    result = {"sha256": hashlib.sha256(content).hexdigest()}
    if path.suffix == ".py":
        # Ignore formatting/comments only; do not execute the candidate source.
        # AST shape can change between Python versions. This is a heuristic,
        # not proof of authorship, infringement, or behavioral equivalence.
        try:
            tree = ast.parse(content, filename=path.name)
        except (SyntaxError, ValueError):
            return result
        normalized = ast.dump(tree, annotate_fields=True, include_attributes=False)
        result["python_ast_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return result


def snapshot(root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "origin_id": ORIGIN_ID,
        "upstream_repository": UPSTREAM,
        "license": "Apache-2.0",
        "python_ast_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "scope": "Selected upstream files, not the whole project or any user's identity.",
        "files": {
            name: fingerprints(contained_file(root, name))
            for name in SOURCE_FILES
        },
    }


def compare(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("origin_id") != ORIGIN_ID:
        raise ValueError("Unsupported manifest or unexpected project origin")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Manifest must contain a non-empty files mapping")
    same_ast_version = manifest.get("python_ast_version") == f"{sys.version_info.major}.{sys.version_info.minor}"
    results = []
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, dict):
            raise ValueError("Invalid manifest file record")
        expected_sha = expected.get("sha256")
        if not isinstance(expected_sha, str) or len(expected_sha) != 64 or any(c not in "0123456789abcdef" for c in expected_sha):
            raise ValueError(f"Invalid SHA-256 for {name}")
        path = contained_file(root, name)
        actual: dict[str, str] = {}
        if not path.is_file():
            status = "missing"
        else:
            actual = fingerprints(path)
            if actual["sha256"] == expected_sha:
                status = "exact"
            elif same_ast_version and "python_ast_sha256" in expected and actual.get("python_ast_sha256") == expected["python_ast_sha256"]:
                status = "python_ast_match"
            else:
                status = "changed"
        results.append({
            "path": name,
            "status": status,
            "expected_sha256": expected_sha,
            "actual_sha256": actual.get("sha256"),
        })
    return {
        "origin_id": ORIGIN_ID,
        "all_exact": all(item["status"] == "exact" for item in results),
        "python_ast_comparison_enabled": same_ast_version,
        "files": results,
        "limitations": "Similarity is not proof of identity, ownership, infringement, or an authenticated timestamp.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "verify"))
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository or suspected copy to examine offline")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Trusted upstream snapshot to compare")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            report = snapshot(args.root)
            exit_code = 0
        else:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            report = compare(args.root, manifest)
            exit_code = 0 if report["all_exact"] else 1
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return exit_code
    except (OSError, ValueError, TypeError) as exc:
        print(f"Provenance check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())