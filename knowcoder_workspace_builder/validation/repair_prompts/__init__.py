"""Repair prompt catalogs for completion and incremental validation."""

from __future__ import annotations

from typing import Any

from . import completion_catalog, incremental_catalog


def resolve_repair_prompt(
    stage: str,
    *,
    mode: str = "completion",
    errors: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    if str(mode) == "incremental":
        return incremental_catalog.resolve(stage, errors=errors, context=context)
    return completion_catalog.resolve(stage, errors=errors, context=context)


__all__ = ["resolve_repair_prompt"]
