"""Persistent task identities layered over Builder Workspaces."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4

from knowcoder_workspace_builder.contracts.errors import MissingStateError, StateConflictError
from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout, validate_session_id


TASK_STATUSES = frozenset({"running", "waiting", "completed", "failed", "stopped"})
BUILDER_STATUS_MAP = {
    "running": "running",
    "needs_problem_confirmation": "waiting",
    "needs_schema_confirmation": "waiting",
    "workspace_ready": "completed",
    "failed": "failed",
    "cancelled": "stopped",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MissingStateError(f"{label} was not found", file=str(path)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateConflictError(f"{label} is not valid JSON", file=str(path), error=str(exc)) from exc
    if not isinstance(value, dict):
        raise StateConflictError(f"{label} must contain a JSON object", file=str(path))
    return value


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    workspace_id: str
    status: str
    stage: str
    version: int
    updated_at: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskRecord":
        task_id = validate_session_id(str(value.get("task_id") or ""))
        workspace_id = validate_session_id(str(value.get("workspace_id") or ""))
        status = str(value.get("status") or "")
        if status not in TASK_STATUSES:
            raise StateConflictError("Task status is invalid", task_id=task_id, status=status)
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise StateConflictError("Task version must be a positive integer", task_id=task_id)
        return cls(
            task_id=task_id,
            workspace_id=workspace_id,
            status=status,
            stage=str(value.get("stage") or ""),
            version=version,
            updated_at=str(value.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "status": self.status,
            "stage": self.stage,
            "version": self.version,
            "updated_at": self.updated_at,
        }


class TaskStore:
    """Own task tokens, minimal state, leases, and cross-process updates."""

    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.locks = SessionLockStore(layout)

    @property
    def root(self) -> Path:
        return self.layout.service_path("tasks", create_parent=True)

    def create(self, workspace_id: str, *, stage: str, version: int) -> tuple[TaskRecord, str]:
        workspace_id = validate_session_id(workspace_id)
        with self.locks.acquire(workspace_id):
            conflict = next(
                (
                    record
                    for record in self.list_records()
                    if record.workspace_id == workspace_id and record.status in {"running", "waiting"}
                ),
                None,
            )
            if conflict is not None:
                raise StateConflictError(
                    "This Workspace already has an active task",
                    workspace_id=workspace_id,
                    task_id=conflict.task_id,
                    status=conflict.status,
                )
            task_id = str(uuid4())
            token = token_urlsafe(32)
            record = TaskRecord(task_id, workspace_id, "running", stage, int(version), _now_iso())
            task_root = self._task_root(task_id)
            _atomic_json(task_root / "state.json", record.to_dict())
            _atomic_json(
                task_root / "secret.json",
                {"continuation_token": token, "token_sha256": hashlib.sha256(token.encode()).hexdigest()},
            )
            return record, token

    def load(self, task_id: str) -> TaskRecord:
        return TaskRecord.from_dict(_read_object(self._task_root(task_id) / "state.json", label="Task state"))

    def by_token(self, continuation_token: str) -> tuple[TaskRecord, str]:
        token = str(continuation_token or "").strip()
        if not token:
            raise MissingStateError("A continuation token is required to identify the task")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for task_root in self._iter_task_roots():
            secret = _read_object(task_root / "secret.json", label="Task token")
            if str(secret.get("token_sha256") or "") == digest:
                return self.load(task_root.name), token
        raise MissingStateError("No task matches this continuation token")

    def token_for(self, task_id: str) -> str:
        secret = _read_object(self._task_root(task_id) / "secret.json", label="Task token")
        token = str(secret.get("continuation_token") or "")
        if not token:
            raise StateConflictError("Task token file is incomplete", task_id=task_id)
        return token

    def find_request(self, request_key: str) -> tuple[TaskRecord, str] | None:
        normalized = str(request_key or "").strip()
        if not normalized:
            return None
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        for task_root in self._iter_task_roots():
            secret = _read_object(task_root / "secret.json", label="Task token")
            if str(secret.get("request_sha256") or "") == digest:
                return self.load(task_root.name), self.token_for(task_root.name)
        return None

    def bind_request(self, task_id: str, request_key: str) -> None:
        normalized = str(request_key or "").strip()
        if not normalized:
            return
        task_root = self._task_root(task_id)
        with self.locks.acquire(task_id):
            secret = _read_object(task_root / "secret.json", label="Task token")
            _atomic_json(
                task_root / "secret.json",
                {**secret, "request_sha256": hashlib.sha256(normalized.encode()).hexdigest()},
            )

    def sync(self, task_id: str, builder: dict[str, Any]) -> TaskRecord:
        status = BUILDER_STATUS_MAP.get(str(builder.get("status") or ""))
        if status is None:
            raise StateConflictError(
                "Builder returned an unsupported task status",
                task_id=task_id,
                builder_status=builder.get("status"),
            )
        version = builder.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise StateConflictError("Builder returned an invalid task version", task_id=task_id)
        with self.locks.acquire(task_id):
            latest = self.load(task_id)
            updated = TaskRecord(
                task_id=latest.task_id,
                workspace_id=latest.workspace_id,
                status=status,
                stage=str(builder.get("stage") or latest.stage),
                version=version,
                updated_at=_now_iso(),
            )
            if latest.version > updated.version:
                raise StateConflictError(
                    "A newer task state already exists",
                    task_id=task_id,
                    current_version=latest.version,
                    submitted_version=updated.version,
                )
            _atomic_json(self._task_root(task_id) / "state.json", updated.to_dict())
        return updated

    def wait_lock(self, task_id: str):
        """Return the non-blocking lock used by one active long wait."""
        normalized = validate_session_id(task_id)
        return self.locks.acquire_nonblocking(f"{normalized}-wait", operation="wait for this task")

    def write_lease(self, task_id: str, **fields: Any) -> None:
        task_root = self._task_root(task_id)
        with self.locks.acquire(task_id):
            path = task_root / "lease.json"
            current = _read_object(path, label="Worker lease") if path.is_file() else {}
            _atomic_json(path, {**current, **fields, "heartbeat_at": _now_iso()})

    def list_records(self) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for root in self._iter_task_roots():
            try:
                records.append(self.load(root.name))
            except (MissingStateError, StateConflictError):
                continue
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records

    def _iter_task_roots(self) -> list[Path]:
        root = self.root
        if not root.is_dir():
            return []
        return [item for item in root.iterdir() if item.is_dir()]

    def _task_root(self, task_id: str) -> Path:
        normalized = validate_session_id(task_id)
        root = self.root / normalized
        resolved = root.resolve(strict=False)
        if resolved.parent != self.root.resolve(strict=False):
            raise StateConflictError("Task path escapes the task store", task_id=normalized)
        return root
