"""Explicit selected-project resolution and process-scoped context."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import UnsafeProjectError


PROJECT_ENVIRONMENTS = ("SCHEMA_WORKSPACE_PROJECT", "KNOWCODER_TARGET_PROJECT_ROOT")


def _unsafe_reason(path: Path) -> str | None:
    if path == Path(path.anchor):
        return "filesystem root"
    home = Path.home().resolve()
    if path == home:
        return "home directory"
    if path in home.parents:
        return "parent of the home directory"
    return None


@dataclass(frozen=True)
class SelectedProject:
    root: Path
    source: str

    @classmethod
    def resolve(cls, value: str | Path | None = None) -> "SelectedProject":
        source = "argument"
        raw: str | Path | None = value
        if raw is None:
            for name in PROJECT_ENVIRONMENTS:
                configured = os.environ.get(name, "").strip()
                if configured:
                    raw = configured
                    source = name
                    break
        if raw is None:
            raw = Path.cwd()
            source = "mcp_working_directory"
        candidate = Path(raw).expanduser()
        try:
            root = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise UnsafeProjectError("Selected project does not exist", path=str(candidate), source=source) from exc
        if not root.is_dir():
            raise UnsafeProjectError("Selected project is not a directory", path=str(root), source=source)
        reason = _unsafe_reason(root)
        if reason:
            raise UnsafeProjectError("Selected project is unsafe", path=str(root), reason=reason, source=source)
        return cls(root=root, source=source)

    def require_match(self, value: str | Path | None) -> Path:
        if value is None or not str(value).strip():
            return self.root
        requested = Path(value).expanduser().resolve(strict=True)
        if requested != self.root:
            raise UnsafeProjectError(
                "Tool call cannot redirect the selected project",
                selected=str(self.root),
                requested=str(requested),
            )
        return self.root
