"""Persistent deletion markers that prevent stale task recreation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def tombstone_path(data_root: Path, session_id: str) -> Path:
    return data_root / "service" / "deleted" / f"{session_id}.json"


def is_deleted(data_root: Path, session_id: str) -> bool:
    return tombstone_path(data_root, session_id).is_file()


def mark_deleted(data_root: Path, session_id: str) -> Path:
    target = tombstone_path(data_root, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    value = {
        "session_id": session_id,
        "deleted_at": datetime.now(UTC).isoformat(),
    }
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
