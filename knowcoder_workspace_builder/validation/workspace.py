"""Final executable knowledge Workspace validation."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.storage.workspace import WorkspaceRepository


def validate_workspace(paths: SessionPaths) -> dict[str, Any]:
    return WorkspaceRepository(paths).validate_ready()
