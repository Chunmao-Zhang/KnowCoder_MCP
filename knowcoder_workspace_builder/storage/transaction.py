"""Atomic Session writes and accepted-version transaction handling."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError

from .paths import SessionPaths


def _encoded_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


class AtomicWriter:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths

    def text(self, target: str | Path, content: str) -> Path:
        path = self.paths.assert_writable(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
        return path

    def bytes(self, target: str | Path, content: bytes) -> Path:
        path = self.paths.assert_writable(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temp.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()
        return path

    def json(self, target: str | Path, value: Any) -> Path:
        return self.text(target, _encoded_json(value))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageBoundaryError("Cannot read valid JSON", path=str(path), error=str(exc)) from exc
