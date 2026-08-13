"""Versioned Session-owned build state persistence."""

from __future__ import annotations

from collections.abc import Callable

from knowcoder_workspace_builder.contracts.errors import MissingStateError, StateConflictError
from knowcoder_workspace_builder.workflow.models import BuildState, now_iso

from .locks import SessionLockStore
from .paths import ProjectLayout, new_session_id
from .transaction import AtomicWriter, read_json


class BuildStateStore:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.locks = SessionLockStore(layout)

    def create(self, question: str, upload_paths: list[str], session_id: str | None = None) -> BuildState:
        session_id = session_id or new_session_id()
        paths = self.layout.session(session_id, create=True)
        state_path = paths.state / "builder.json"
        with self.locks.acquire(session_id):
            if state_path.exists():
                raise StateConflictError("Session already exists", session_id=session_id)
            state = BuildState(session_id=session_id, question=question, upload_paths=list(upload_paths))
            AtomicWriter(paths).json(state_path, state.to_dict())
            return state

    def load(self, session_id: str) -> BuildState:
        paths = self.layout.session(session_id)
        state_path = paths.state / "builder.json"
        if not state_path.is_file():
            raise MissingStateError("Builder Session does not exist", session_id=session_id)
        value = read_json(state_path)
        return BuildState.from_dict(value)

    def update(
        self,
        session_id: str,
        expected_version: int,
        mutate: Callable[[BuildState], BuildState | None],
    ) -> BuildState:
        paths = self.layout.session(session_id)
        state_path = paths.state / "builder.json"
        with self.locks.acquire(session_id):
            if not state_path.is_file():
                raise MissingStateError("Builder Session does not exist", session_id=session_id)
            current = BuildState.from_dict(read_json(state_path))
            if current.version != expected_version:
                raise StateConflictError(
                    "Builder state changed before this result could be accepted",
                    session_id=session_id,
                    expected_version=expected_version,
                    current_version=current.version,
                )
            updated = mutate(current) or current
            if updated.session_id != current.session_id:
                raise StateConflictError("State mutation changed Session ownership", session_id=session_id)
            updated.version = current.version + 1
            updated.updated_at = now_iso()
            AtomicWriter(paths).json(state_path, updated.to_dict())
            return updated

    def list_states(self, limit: int = 50) -> list[BuildState]:
        if limit < 1:
            return []
        sessions_root = self.layout.data_root / "sessions"
        if not sessions_root.is_dir():
            return []
        states: list[BuildState] = []
        for path in sessions_root.glob("*/intermediate/builder.json"):
            try:
                states.append(BuildState.from_dict(read_json(path)))
            except (MissingStateError, TypeError, ValueError):
                continue
        states.sort(key=lambda item: item.updated_at, reverse=True)
        return states[:limit]
