"""Append-only ordered Builder invocation and state event persistence."""

from __future__ import annotations

import json
import os
from typing import Any

from knowcoder_workspace_builder.contracts.events import InvocationEvent

from .locks import SessionLockStore
from .paths import ProjectLayout
from .transaction import AtomicWriter, read_json


class EventStore:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.locks = SessionLockStore(layout)

    def append(self, session_id: str, **fields: Any) -> InvocationEvent:
        paths = self.layout.session(session_id, create=True)
        sequence_path = paths.state / "event_sequence.json"
        event_path = paths.events / "events.jsonl"
        with self.locks.acquire(session_id):
            sequence = 0
            if sequence_path.exists():
                value = read_json(sequence_path)
                sequence = int(value.get("sequence") or 0) if isinstance(value, dict) else 0
            event = InvocationEvent(session_id=session_id, sequence=sequence + 1, **fields)
            line = json.dumps(event.to_dict(include_private=True), ensure_ascii=False, sort_keys=True) + "\n"
            descriptor = os.open(event_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            AtomicWriter(paths).json(sequence_path, {"sequence": event.sequence})
            return event

    def read(self, session_id: str, *, after: int = 0, public_only: bool = False) -> list[dict[str, Any]]:
        paths = self.layout.session(session_id)
        event_path = paths.events / "events.jsonl"
        if not event_path.is_file():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid event log line {number}: {exc}") from exc
            if int(record.get("sequence") or 0) <= after:
                continue
            if public_only:
                if record.get("visibility") != "public":
                    continue
                record.pop("private", None)
            records.append(record)
        records.sort(key=lambda item: int(item["sequence"]))
        return records
