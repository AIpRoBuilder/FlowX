# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Added 2026-11-16: cover plugin-saved environment variables in skill processes.
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import runpy

SETTINGS = Path(__file__).resolve().parents[1] / "skills" / "flowx-workflows" / "scripts" / "plugin_settings.py"
SPEC = importlib.util.spec_from_file_location("flowx_plugin_settings", SETTINGS)
assert SPEC is not None and SPEC.loader is not None
plugin_settings = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin_settings)


def test_environment_filters_reserved_names_and_unsafe_values():
    assert plugin_settings.environment({}) == {}
    assert plugin_settings.environment({"env": "not a map"}) == {}
    assert plugin_settings.environment({"env": {
        "OPENAI_BASE_URL": "https://gateway.test/v1",
        "OPENAI_API_KEY": "secret",
        "FLOWX_LLM_API_KEY": "ignored",
        "FLOWX_WORKSPACE": "/ignored",
        "__proto__": "ignored",
        "bad-name": "ignored",
        "9LEADING": "ignored",
        "BLANK": "",
        "CONTROL": "line\nbreak",
        "TAB": "tab\tvalue",
        "TOO_LONG": "x" * 4097,
        1: "ignored",
        "NOT_A_STRING": 1,
    }}) == {
        "OPENAI_BASE_URL": "https://gateway.test/v1",
        "OPENAI_API_KEY": "secret",
        "BLANK": "",
    }


def test_apply_exports_saved_environment_and_keeps_the_saved_api_key(monkeypatch, tmp_path):
    home = tmp_path / "dsh"
    directory = home / "plugins" / "deepseek-harness-flowx"
    directory.mkdir(parents=True, mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = directory / "settings.json"
    settings.write_text(json.dumps({
        "python": None,
        "workspace": str(workspace),
        "apiKey": "saved-key",
        "env": {
            "OPENAI_BASE_URL": "https://gateway.test/v1",
            "FLOWX_MEDIA_DRY_RUN": "1",
            "FLOWX_LLM_API_KEY": "ignored-by-reservation",
        },
    }), encoding="utf-8")
    settings.chmod(0o600)

    for name in ("OPENAI_BASE_URL", "FLOWX_MEDIA_DRY_RUN", "FLOWX_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FLOWX_MEDIA_DRY_RUN", "0")  # saved values override inherited values
    monkeypatch.setenv("DSH_HOME", str(home))

    assert plugin_settings.apply(None) == str(workspace)
    assert os.environ["OPENAI_BASE_URL"] == "https://gateway.test/v1"
    assert os.environ["FLOWX_MEDIA_DRY_RUN"] == "1"
    assert os.environ["FLOWX_LLM_API_KEY"] == "saved-key"


def test_cli_reports_environment_names_without_values(monkeypatch, tmp_path, capsys):
    home = tmp_path / "dsh"
    directory = home / "plugins" / "deepseek-harness-flowx"
    directory.mkdir(parents=True, mode=0o700)
    settings = directory / "settings.json"
    settings.write_text(json.dumps({
        "python": None,
        "workspace": str(tmp_path),
        "apiKey": "cli-secret",
        "env": {"OPENAI_BASE_URL": "https://gateway.test/v1", "OPENAI_API_KEY": "env-secret"},
    }), encoding="utf-8")
    settings.chmod(0o600)
    monkeypatch.setenv("DSH_HOME", str(home))

    runpy.run_path(str(SETTINGS), run_name="__main__")
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["api_key_set"] is True
    assert data["env_keys"] == ["OPENAI_API_KEY", "OPENAI_BASE_URL"]
    assert "cli-secret" not in output
    assert "env-secret" not in output
    assert "gateway.test" not in output
