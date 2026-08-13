from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from knowcoder_workspace_builder.config import default_config_path, load_settings
from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.cli import doctor


ROOT = Path(__file__).resolve().parents[3]


def _config(path: Path) -> None:
    path.write_text(
        'RESEARCH_MODEL = {"api_key": "research-key", "base_url": "https://research.test/v1", "model": "research-model"}\n'
        'EXTRACTION_MODEL = {"api_key": "extract-key", "base_url": "https://extract.test/v1", "model": "extract-model"}\n'
        'SERPER_API_KEY = "search-key"\n',
        encoding="utf-8",
    )


def test_example_config_declares_only_required_services() -> None:
    example = (ROOT / "config.py.example").read_text(encoding="utf-8")
    assert "RESEARCH_MODEL" in example
    assert "EXTRACTION_MODEL" in example
    assert "SERPER_API_KEY" in example
    assert "api_key" in example and "base_url" in example and "model" in example


def test_configuration_loads_and_maps_runtime_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    _config(path)
    settings = load_settings(path)
    environment = settings.environment()
    assert environment["SCHEMA_AGENT_MODEL"] == "knowcoder/research-model"
    assert environment["KNOWCODER_BASE_URL"] == "https://research.test/v1"
    assert environment["UNSTRUCTURED_EXTRACTION_MODEL"] == "extract-model"
    assert environment["SERPER_API_KEY"] == "search-key"


def test_configuration_fails_fast_with_field_and_file(tmp_path: Path) -> None:
    path = tmp_path / "config.py"
    path.write_text(
        'RESEARCH_MODEL = {"api_key": "", "base_url": "https://research.test/v1", "model": "model"}\n'
        'EXTRACTION_MODEL = {"api_key": "key", "base_url": "https://extract.test/v1", "model": "model"}\n'
        'SERPER_API_KEY = "search-key"\n',
        encoding="utf-8",
    )
    with pytest.raises(ContractError) as caught:
        load_settings(path)
    assert caught.value.detail.context["field"] == "api_key"
    assert caught.value.detail.context["section"] == "RESEARCH_MODEL"


def test_platform_default_config_location(monkeypatch) -> None:
    monkeypatch.delenv("KNOWCODER_MCP_CONFIG", raising=False)
    if os.name == "nt":
        assert default_config_path().name == "config.py"
    else:
        assert default_config_path() == Path.home() / ".config" / "knowcoder-mcp" / "config.py"


def test_installers_use_uv_tool_and_copy_the_example() -> None:
    mac = (ROOT / "scripts" / "install_mcp_runtime.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "install_mcp_runtime.ps1").read_text(encoding="utf-8")
    assert "uv tool install --force --python 3.12" in mac and "config.py.example" in mac
    assert "uv tool install --force --python 3.12" in windows and "config.py.example" in windows


def test_local_doctor_does_not_call_external_services(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "config.py"
    config_path.write_text((ROOT / "config.py.example").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("KNOWCODER_MCP_CONFIG", str(config_path))

    with patch("knowcoder_workspace_builder.cli._check_model") as model_check, patch(
        "knowcoder_workspace_builder.cli.httpx.post"
    ) as search_call:
        assert doctor(local_only=True) == 0

    model_check.assert_not_called()
    search_call.assert_not_called()
    output = capsys.readouterr().out
    assert "PASS local installation" in output
    assert "WARN configuration incomplete" in output
    assert "PASS MCP initialization: 6 tools" in output
