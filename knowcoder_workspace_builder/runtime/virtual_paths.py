"""Translate current-Session paths for protected Harness file tools."""

from __future__ import annotations

from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError

from .session_context import active_session_paths


VIRTUAL_ROOT = "/.knowcoder_workspace"


def virtual_session_path(relative: str) -> str:
    """Return a protected-Harness path backed by the active Session root."""
    candidate = Path(str(relative or "").strip())
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        raise StorageBoundaryError("Virtual Session path must be a safe relative path", path=str(relative))
    return f"{VIRTUAL_ROOT}/{candidate.as_posix()}"


def virtual_path_for(run: str | Path, path: str | Path) -> str:
    paths = active_session_paths()
    run_root = Path(run).expanduser().resolve(strict=True)
    if run_root != paths.root.resolve(strict=True):
        raise StorageBoundaryError("Harness run does not match the active Session", run=str(run_root))
    target = Path(path).expanduser().resolve(strict=False)
    try:
        relative = target.relative_to(paths.root.resolve(strict=True))
    except ValueError as exc:
        raise StorageBoundaryError("Virtual path is outside the active Session", path=str(target)) from exc
    return virtual_session_path(relative.as_posix()) if relative.parts else VIRTUAL_ROOT


def resolve_virtual_path(value: str | Path) -> Path:
    paths = active_session_paths()
    text = str(value or "").strip()
    if not text.startswith(VIRTUAL_ROOT + "/"):
        raise StorageBoundaryError("Harness path must use the current Session virtual root", path=text)
    relative = text.removeprefix(VIRTUAL_ROOT + "/")
    return paths.assert_writable(paths.root / relative)
