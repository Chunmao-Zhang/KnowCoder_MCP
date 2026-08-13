"""Attempt ownership, cancellation, and terminal-state persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from knowcoder_workspace_builder.contracts.errors import MissingStateError, StateConflictError

from .locks import SessionLockStore
from .paths import ProjectLayout
from .transaction import AtomicWriter, read_json


class AttemptStore:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.locks = SessionLockStore(layout)

    def start(self, session_id: str, stage: str, number: int, *, attempt_id: str | None = None) -> dict[str, Any]:
        paths = self.layout.session(session_id, create=True)
        attempt_id = str(UUID(attempt_id)) if attempt_id else str(uuid4())
        record = {
            "attempt_id": attempt_id,
            "session_id": session_id,
            "stage": stage,
            "number": number,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "error": None,
        }
        with self.locks.acquire(session_id):
            AtomicWriter(paths).json(paths.attempts / f"{attempt_id}.json", record)
        return record

    def finish(self, session_id: str, attempt_id: str, status: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise StateConflictError("Attempt has invalid terminal status", status=status)
        paths = self.layout.session(session_id)
        path = paths.attempts / f"{attempt_id}.json"
        with self.locks.acquire(session_id):
            if not path.is_file():
                raise MissingStateError("Attempt does not exist", attempt_id=attempt_id)
            record = read_json(path)
            if record.get("status") == status:
                return record
            if record.get("status") != "running":
                raise StateConflictError("Attempt is already terminal", attempt_id=attempt_id)
            record.update(
                status=status,
                error=error,
                finished_at=datetime.now(UTC).isoformat(),
            )
            AtomicWriter(paths).json(path, record)
            return record

    def is_active(self, session_id: str, attempt_id: str) -> bool:
        path = self.layout.session(session_id).attempts / f"{attempt_id}.json"
        if not path.is_file():
            return False
        value = read_json(path)
        return isinstance(value, dict) and value.get("status") == "running"
