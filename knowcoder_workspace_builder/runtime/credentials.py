"""Resolve service credentials from the active project without logging secrets."""

from __future__ import annotations

import os
from pathlib import Path


def configured_secret(value: object) -> str:
    """Return a concrete secret and reject unresolved environment placeholders."""
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text


def read_project_env_value(name: str) -> str:
    """Read one value from the active project .env without changing process state."""
    configured_roots = [
        value
        for value in (
            os.environ.get("SCHEMA_WORKSPACE_PROJECT", ""),
            os.environ.get("KNOWCODER_BUILDER_ROOT", ""),
            os.environ.get("OTOLOGY_BUILDER_ROOT", ""),
        )
        if value
    ]
    candidates = [
        *(Path(value).expanduser() for value in configured_roots),
        Path(__file__).resolve().parents[2],
        Path.cwd(),
    ]
    for root in candidates:
        env_path = root / ".env"
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith(f"{name}="):
                    continue
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
        except OSError:
            continue
    return ""


def service_api_key(name: str, configured_value: object = "") -> str:
    """Resolve a service API key, preferring the active project's current .env."""
    return (
        configured_secret(read_project_env_value(name))
        or configured_secret(os.environ.get(name, ""))
        or configured_secret(configured_value)
    )
