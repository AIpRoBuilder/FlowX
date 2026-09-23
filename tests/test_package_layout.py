from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_PACKAGES = {
    "core": "flowx_core",
    "mcp": "flowx_mcp",
    "sdk": "flowx_sdk",
    "a2a": "flowx_a2a",
}


def test_each_component_uses_a_local_package_root() -> None:
    for component, package_name in COMPONENT_PACKAGES.items():
        source_root = REPOSITORY_ROOT / "packages" / component / package_name
        assert source_root.is_dir(), f"{component} must keep Python sources under {source_root}"


def test_components_do_not_use_a_src_layout() -> None:
    for component in COMPONENT_PACKAGES:
        src_root = REPOSITORY_ROOT / "packages" / component / "src"
        assert not src_root.exists(), f"{component} must not nest sources under {src_root}"