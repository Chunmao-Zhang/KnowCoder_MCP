"""User-level Python configuration loading and runtime validation."""

from __future__ import annotations

import os
import runpy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError


CONFIG_PATH_ENV = "KNOWCODER_MCP_CONFIG"


def default_config_path() -> Path:
    override = str(os.environ.get(CONFIG_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "knowcoder-mcp" / "config.py"


@dataclass(frozen=True)
class ModelSettings:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class KnowCoderSettings:
    research: ModelSettings
    extraction: ModelSettings
    serper_api_key: str

    def environment(self) -> dict[str, str]:
        provider = "knowcoder"
        return {
            "SCHEMA_AGENT_MODEL": f"{provider}/{self.research.model}",
            "KNOWCODER_API_KEY": self.research.api_key,
            "KNOWCODER_BASE_URL": self.research.base_url,
            "UNSTRUCTURED_EXTRACTION_API_KEY": self.extraction.api_key,
            "UNSTRUCTURED_EXTRACTION_BASE_URL": self.extraction.base_url,
            "UNSTRUCTURED_EXTRACTION_MODEL": self.extraction.model,
            "SERPER_API_KEY": self.serper_api_key,
        }


def _required_text(mapping: dict[str, Any], field: str, *, section: str) -> str:
    value = str(mapping.get(field) or "").strip()
    if not value:
        raise ContractError(
            "KnowCoder configuration is incomplete",
            file=str(default_config_path()),
            section=section,
            field=field,
        )
    return value


def _model_settings(namespace: dict[str, Any], name: str) -> ModelSettings:
    value = namespace.get(name)
    if not isinstance(value, dict):
        raise ContractError(
            "KnowCoder configuration requires a model section",
            file=str(default_config_path()),
            section=name,
        )
    return ModelSettings(
        api_key=_required_text(value, "api_key", section=name),
        base_url=_required_text(value, "base_url", section=name).rstrip("/"),
        model=_required_text(value, "model", section=name),
    )


def load_settings(path: str | Path | None = None) -> KnowCoderSettings:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.is_file():
        raise ContractError(
            "KnowCoder configuration file was not found",
            file=str(config_path),
            action="Copy config.py.example to this location and fill every required value.",
        )
    namespace = runpy.run_path(str(config_path))
    return KnowCoderSettings(
        research=_model_settings(namespace, "RESEARCH_MODEL"),
        extraction=_model_settings(namespace, "EXTRACTION_MODEL"),
        serper_api_key=_required_text(namespace, "SERPER_API_KEY", section="root"),
    )


def apply_settings(settings: KnowCoderSettings) -> None:
    for name, value in settings.environment().items():
        os.environ[name] = value
