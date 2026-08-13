"""Current-Session path resolution for Builder Agent tools."""

from __future__ import annotations

from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import VIRTUAL_ROOT, resolve_virtual_path
from knowcoder_workspace_builder.storage.paths import is_within


def resolve_path(value: str | Path) -> Path:
    text = str(value or "").strip()
    if not text:
        raise StorageBoundaryError("Tool path is required")
    if text.startswith(VIRTUAL_ROOT + "/"):
        return resolve_virtual_path(text)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise StorageBoundaryError("Tool path must be an absolute current-Session path", path=text)
    resolved = candidate.resolve(strict=False)
    paths = active_session_paths()
    if not is_within(resolved, paths.root.resolve(strict=True)):
        raise StorageBoundaryError("Tool path is outside the current Session", path=str(resolved))
    return resolved
