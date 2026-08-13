"""Bind a protected Harness process to one `.knowcoder_workspace` Session."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError
from knowcoder_workspace_builder.storage.paths import DATA_DIRECTORY, ProjectLayout, SessionPaths


SESSION_ROOT_ENV = "SCHEMA_WORKSPACE_SESSION_ROOT"
SESSION_ID_ENV = "SCHEMA_WORKSPACE_SESSION_ID"
ATTEMPT_ID_ENV = "SCHEMA_WORKSPACE_ATTEMPT_ID"
HARNESS_ARTIFACTS_ROOT_ENV = "HARNESS_ARTIFACTS_ROOT"


def _validated_session_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise StorageBoundaryError("Harness Session root is not a directory", path=str(root))
    if root.parent.name != "sessions" or root.parent.parent.name != DATA_DIRECTORY:
        raise StorageBoundaryError("Harness Session root is outside `.knowcoder_workspace/sessions`", path=str(root))
    return root


def active_session_paths() -> SessionPaths:
    configured = os.environ.get(SESSION_ROOT_ENV, "").strip()
    if not configured:
        raise StorageBoundaryError("Harness Session context is missing", environment=SESSION_ROOT_ENV)
    root = _validated_session_root(configured)
    project = root.parents[2]
    paths = ProjectLayout(project).session(root.name)
    if paths.root.resolve(strict=True) != root:
        raise StorageBoundaryError("Harness Session context does not match its selected project", path=str(root))
    configured_id = os.environ.get(SESSION_ID_ENV, "").strip()
    if configured_id and configured_id != paths.session_id:
        raise StorageBoundaryError(
            "Harness Session ID does not match its Session root",
            configured=configured_id,
            actual=paths.session_id,
        )
    return paths


@contextmanager
def harness_session_environment(paths: SessionPaths, attempt_id: str) -> Iterator[Mapping[str, str]]:
    """Bind one protected Harness invocation directly to its Session root."""
    if not str(attempt_id).strip():
        raise StorageBoundaryError("Harness invocation requires an attempt ID")
    environment = {
        "KNOWCODER_TARGET_PROJECT_ROOT": str(paths.root),
        "HARNESS_RUN_DIR": str(paths.root),
        HARNESS_ARTIFACTS_ROOT_ENV: "/" + paths.intermediate.relative_to(paths.root).as_posix(),
        SESSION_ROOT_ENV: str(paths.root),
        SESSION_ID_ENV: paths.session_id,
        ATTEMPT_ID_ENV: attempt_id,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        yield environment
    finally:
        runtime_tmp = paths.intermediate / "runtime_tmp"
        if runtime_tmp.is_dir():
            shutil.rmtree(runtime_tmp)
