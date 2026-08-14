"""Attempt-level tool objectives, duplicate detection, and terminal status."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from knowcoder_workspace_builder.contracts.errors import (
    ContractError,
    StateConflictError,
)

from .locks import SessionLockStore
from .paths import ProjectLayout, SessionPaths
from .transaction import AtomicWriter, read_json


class ToolCallLedger:
    def __init__(self, paths: SessionPaths, attempt_id: str) -> None:
        self.paths = paths
        self.attempt_id = attempt_id
        self.path = paths.attempts / attempt_id / "tool_calls.json"
        self.locks = SessionLockStore(ProjectLayout(paths.project))

    def _records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        value = read_json(self.path)
        if not isinstance(value, dict) or not isinstance(value.get("calls"), list):
            raise ContractError("Tool call ledger is invalid", attempt_id=self.attempt_id)
        return [dict(item) for item in value["calls"] if isinstance(item, dict)]

    def completed_count(self, tool_name: str) -> int:
        return sum(
            1
            for item in self._records()
            if item.get("tool") == tool_name and item.get("status") == "completed"
        )

    def finished_count(self, tool_name: str) -> int:
        return sum(
            1
            for item in self._records()
            if item.get("tool") == tool_name and item.get("status") in {"completed", "failed"}
        )

    @staticmethod
    def signature(tool_name: str, arguments: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def start(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        objective: str,
    ) -> str:
        normalized_objective = str(objective or "").strip()
        if not normalized_objective:
            raise ContractError("Every tool call requires a current objective", tool=tool_name)
        signature = self.signature(tool_name, arguments)
        with self.locks.acquire(self.paths.session_id):
            records = self._records()
            if any(
                item.get("signature") == signature and item.get("status") != "failed"
                for item in records
            ):
                raise StateConflictError("Equivalent tool call already exists in this attempt", tool=tool_name)
            records.append(
                {
                    "signature": signature,
                    "tool": tool_name,
                    "objective": normalized_objective,
                    "status": "running",
                    "started_at": datetime.now(UTC).isoformat(),
                    "finished_at": None,
                }
            )
            AtomicWriter(self.paths).json(self.path, {"calls": records})
        return signature

    def finish(self, signature: str, status: str, *, error: str = "") -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("Tool call terminal status is invalid")
        with self.locks.acquire(self.paths.session_id):
            records = self._records()
            record = next(
                (
                    item
                    for item in reversed(records)
                    if item.get("signature") == signature and item.get("status") == "running"
                ),
                None,
            )
            if record is None:
                raise StateConflictError("Tool call ledger record is missing", signature=signature)
            record["status"] = status
            record["finished_at"] = datetime.now(UTC).isoformat()
            if error:
                record["error"] = error
            AtomicWriter(self.paths).json(self.path, {"calls": records})


class SearchLedger:
    def __init__(self, paths: SessionPaths, attempt_id: str) -> None:
        self.paths = paths
        self.path = paths.research / f"searches-{attempt_id}.json"
        self.locks = SessionLockStore(ProjectLayout(paths.project))

    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        value = read_json(self.path)
        if not isinstance(value, dict) or not isinstance(value.get("searches"), list):
            raise ContractError("Search ledger is invalid", path=str(self.path))
        return [dict(item) for item in value["searches"] if isinstance(item, dict)]

    def find(self, signature: str) -> dict[str, Any] | None:
        return next((item for item in self.records() if item.get("signature") == signature), None)

    def append(self, value: dict[str, Any]) -> None:
        with self.locks.acquire(self.paths.session_id):
            records = self.records()
            records.append(value)
            AtomicWriter(self.paths).json(self.path, {"searches": records})


class FetchLedger:
    """Attempt-level bindings created by explicit webpage fetch calls."""

    def __init__(self, paths: SessionPaths, attempt_id: str) -> None:
        self.paths = paths
        self.path = paths.research / f"fetches-{attempt_id}.json"
        self.locks = SessionLockStore(ProjectLayout(paths.project))

    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        value = read_json(self.path)
        if not isinstance(value, dict) or not isinstance(value.get("fetches"), list):
            raise ContractError("Web fetch ledger is invalid", path=str(self.path))
        return [dict(item) for item in value["fetches"] if isinstance(item, dict)]

    def append(self, value: dict[str, Any]) -> None:
        with self.locks.acquire(self.paths.session_id):
            records = self.records()
            records.append(value)
            AtomicWriter(self.paths).json(self.path, {"fetches": records})
