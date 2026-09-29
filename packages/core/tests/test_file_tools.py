from pathlib import Path

import pytest

from flowx_core.tools.file_tools import parse_json_file


def test_parse_json_file_loads_json_from_its_directory(tmp_path: Path) -> None:
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    (config_directory / "pipeline.json").write_text('{"nodes": []}', encoding="utf-8")

    assert parse_json_file("pipeline.json", config_directory) == {"nodes": []}

    with pytest.raises(ValueError, match="must be located under"):
        parse_json_file("../outside.json", config_directory)