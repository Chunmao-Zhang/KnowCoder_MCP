"""Cross-platform write-boundary helpers for Harness tools."""

from __future__ import annotations

import os
from pathlib import Path

TARGET_ROOT_ENV = "KNOWCODER_TARGET_PROJECT_ROOT"
RUN_ROOT_ENV = "HARNESS_RUN_DIR"
WORKSPACE_ROOT_NAME = ".knowcoder_workspace"
SESSION_ROOT_DIRECTORIES = frozenset({"workspace", "intermediate"})


class WriteBoundaryError(ValueError):
    """Raised when a requested write falls outside the selected workspace."""


def selected_project_root() -> Path:
    value = str(os.environ.get(TARGET_ROOT_ENV) or "").strip()
    if not value:
        raise WriteBoundaryError(f"{TARGET_ROOT_ENV} is required")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise WriteBoundaryError(f"Selected project directory does not exist: {root}")
    return root


def workspace_write_root() -> Path:
    project = selected_project_root()
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == project:
        return project
    candidate = project / WORKSPACE_ROOT_NAME
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise WriteBoundaryError(
            f"{WORKSPACE_ROOT_NAME} must resolve inside the selected project: {candidate}"
        ) from exc
    return resolved


def runtime_temporary_root() -> Path:
    project = selected_project_root()
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == project:
        return project / "intermediate" / "runtime_tmp"
    return workspace_write_root() / "runtime_tmp"


def execution_write_root() -> Path:
    """Return the only directory model-written code may modify."""
    project = selected_project_root()
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == project:
        return project / "intermediate"
    return workspace_write_root()


def resolve_tool_path(file_path: str) -> Path:
    value = str(file_path or "").strip()
    project = selected_project_root()
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    session_is_project = bool(session_root) and Path(session_root).expanduser().resolve() == project
    if value.startswith("/.knowcoder_workspace/"):
        relative = value.removeprefix("/.knowcoder_workspace/").lstrip("/")
        return (project / relative if session_is_project else project / value.lstrip("/")).resolve()
    if value.startswith("/workspaces/"):
        relative = value.removeprefix("/workspaces/").lstrip("/")
        return (project / WORKSPACE_ROOT_NAME / "workspaces" / relative).resolve()
    host_path = Path(value).expanduser()
    if host_path.is_absolute():
        resolved = host_path.resolve()
        try:
            resolved.relative_to(project)
        except ValueError as exc:
            raise WriteBoundaryError(
                f"absolute host path is outside the selected project: {resolved}"
            ) from exc
        return resolved
    if not value.startswith("/"):
        raise WriteBoundaryError("file path must be an absolute virtual path")
    return (project / value.lstrip("/")).resolve()


def require_workspace_write_path(file_path: str) -> Path:
    resolved = resolve_tool_path(file_path)
    root = workspace_write_root()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise WriteBoundaryError(
            f"write path must resolve inside {root}: {resolved}"
        ) from exc
    project = selected_project_root()
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == project:
        if not relative.parts or relative.parts[0] not in SESSION_ROOT_DIRECTORIES:
            raise WriteBoundaryError(
                "Session writes must stay under workspace or intermediate"
            )
    return resolved


def require_active_run_root() -> Path:
    value = str(os.environ.get(RUN_ROOT_ENV) or "").strip()
    if not value:
        raise WriteBoundaryError(f"{RUN_ROOT_ENV} is required for this tool")
    run_root = Path(value).expanduser().resolve()
    write_root = workspace_write_root()
    try:
        run_root.relative_to(write_root)
    except ValueError as exc:
        raise WriteBoundaryError(
            f"active run must resolve inside {write_root}: {run_root}"
        ) from exc
    return run_root
