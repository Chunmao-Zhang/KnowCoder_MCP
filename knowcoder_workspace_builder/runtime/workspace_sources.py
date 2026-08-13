"""Persist source records in the current Session research area."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.storage.sources import SOURCE_CATEGORIES, SourceRepository

from .session_context import active_session_paths


def _require_run(run: str | Path) -> None:
    paths = active_session_paths()
    if Path(run).expanduser().resolve(strict=True) != paths.root.resolve(strict=True):
        raise ContractError("Source operation belongs to a different Session", run=str(run))


def ensure_source_dirs(run: str | Path) -> None:
    _require_run(run)
    paths = active_session_paths()
    for category in SOURCE_CATEGORIES:
        (paths.sources / category).mkdir(parents=True, exist_ok=True)


def source_category_dir(run: str | Path, category: str) -> Path:
    _require_run(run)
    if category not in SOURCE_CATEGORIES:
        raise ContractError("Unknown source category", category=category)
    path = active_session_paths().sources / category
    path.mkdir(parents=True, exist_ok=True)
    return path


def source_records(run: str | Path) -> list[dict[str, Any]]:
    _require_run(run)
    return SourceRepository(active_session_paths()).list()


def register_source_record(run: str | Path, category: str, record: dict[str, Any]) -> dict[str, Any]:
    _require_run(run)
    if category not in SOURCE_CATEGORIES:
        raise ContractError("Unknown source category", category=category)
    return SourceRepository(active_session_paths()).register(category, record)


def register_source_version(
    run: str | Path,
    category: str,
    record: dict[str, Any],
    *,
    supersedes: list[str],
) -> dict[str, Any]:
    _require_run(run)
    return SourceRepository(active_session_paths()).register_version(
        category,
        record,
        supersedes=supersedes,
    )
