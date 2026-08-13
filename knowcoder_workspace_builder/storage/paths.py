"""Canonical selected-project, Session, internal, and public Workspace layout."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError

from .project import SelectedProject
from .tombstones import is_deleted


DATA_DIRECTORY = ".knowcoder_workspace"
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
IGNORE_CONTENT = "# KnowCoder Workspace runtime data is local to this project.\n*\n!.gitignore\n"
REQUIRED_IGNORE_RULES = frozenset({"*", "!.gitignore"})
SESSION_ROOT_DIRECTORIES = frozenset({"workspace", "intermediate"})


def new_session_id() -> str:
    return str(uuid4())


def validate_session_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not SESSION_PATTERN.fullmatch(normalized):
        raise StorageBoundaryError("Invalid Session ID", session_id=normalized)
    return normalized


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class SessionPaths:
    project: Path
    data_root: Path
    session_id: str
    root: Path
    workspace: Path
    intermediate: Path

    @property
    def sources(self) -> Path:
        return self.intermediate / "sources"

    @property
    def attempts(self) -> Path:
        return self.intermediate / "attempts"

    @property
    def stages(self) -> Path:
        return self.intermediate / "stages"

    @property
    def research(self) -> Path:
        return self.intermediate

    @property
    def state(self) -> Path:
        return self.intermediate

    @property
    def events(self) -> Path:
        return self.intermediate

    def assert_writable(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved_parent = candidate.parent.resolve(strict=False)
        resolved = resolved_parent / candidate.name
        if not is_within(resolved, self.root):
            raise StorageBoundaryError(
                "Write target is outside the current Session",
                session_id=self.session_id,
                target=str(resolved),
            )
        relative = resolved.relative_to(self.root)
        if not relative.parts or relative.parts[0] not in SESSION_ROOT_DIRECTORIES:
            raise StorageBoundaryError(
                "Session writes must stay under workspace or intermediate",
                session_id=self.session_id,
                target=str(resolved),
            )
        return resolved

    def assert_root_layout(self) -> None:
        if not self.root.exists():
            return
        if not self.root.is_dir():
            raise StorageBoundaryError("Session root is not a directory", path=str(self.root))
        entries = {item.name: item for item in self.root.iterdir()}
        unexpected = sorted(set(entries) - SESSION_ROOT_DIRECTORIES)
        invalid = sorted(
            name
            for name, path in entries.items()
            if name in SESSION_ROOT_DIRECTORIES and not path.is_dir()
        )
        if unexpected or invalid:
            raise StorageBoundaryError(
                "Session root must contain only workspace and intermediate directories",
                session_id=self.session_id,
                unexpected=unexpected,
                invalid=invalid,
            )

    def relative_to_project(self, value: str | Path) -> str:
        path = Path(value).resolve(strict=False)
        if not is_within(path, self.project):
            raise StorageBoundaryError("Path is outside the selected project", target=str(path))
        return path.relative_to(self.project).as_posix()


class ProjectLayout:
    def __init__(self, project: SelectedProject | str | Path) -> None:
        self.selected = project if isinstance(project, SelectedProject) else SelectedProject.resolve(project)
        self.project = self.selected.root
        self.data_root = self.project / DATA_DIRECTORY

    def ensure_data_root(self) -> Path:
        self.data_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        ignore = self.data_root / ".gitignore"
        if not ignore.exists():
            temporary = self.data_root / f".gitignore.{uuid4().hex}.tmp"
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(IGNORE_CONTENT)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, ignore)
            finally:
                temporary.unlink(missing_ok=True)
        rules = {
            line.strip()
            for line in ignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if not REQUIRED_IGNORE_RULES.issubset(rules):
            raise StorageBoundaryError(
                "Runtime ignore file must ignore Workspace data while retaining itself",
                path=str(ignore),
                required_rules=sorted(REQUIRED_IGNORE_RULES),
            )
        return self.data_root

    def service_path(self, *parts: str, create_parent: bool = False) -> Path:
        self.ensure_data_root()
        target = self.data_root.joinpath("service", *parts)
        if not is_within(target.resolve(strict=False), self.data_root):
            raise StorageBoundaryError("Service path escapes runtime root", path=str(target))
        if create_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def session(self, session_id: str, *, create: bool = False) -> SessionPaths:
        session_id = validate_session_id(session_id)
        data_root = self.ensure_data_root() if create else self.data_root
        if is_deleted(data_root, session_id):
            raise StorageBoundaryError("Session was deleted and cannot be recreated", session_id=session_id)
        root = data_root / "sessions" / session_id
        paths = SessionPaths(
            project=self.project,
            data_root=data_root,
            session_id=session_id,
            root=root,
            workspace=root / "workspace",
            intermediate=root / "intermediate",
        )
        if create:
            for directory in (
                paths.workspace,
                paths.intermediate,
                paths.sources,
                paths.attempts,
                paths.stages,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        paths.assert_root_layout()
        return paths
