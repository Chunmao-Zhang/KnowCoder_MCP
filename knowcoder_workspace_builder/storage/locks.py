"""Session-scoped cooperative process and thread locks."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Iterator

from knowcoder_workspace_builder.contracts.errors import StateConflictError

from .paths import ProjectLayout, validate_session_id


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD_LOCKS = threading.local()


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _held_locks() -> dict[str, tuple[int, int]]:
    held = getattr(_HELD_LOCKS, "locks", None)
    if held is None:
        held = {}
        _HELD_LOCKS.locks = held
    return held


@dataclass
class ProjectProcessLock:
    """Hold one non-blocking service-instance lock for a selected project."""

    path: str
    descriptor: int
    service_name: str
    released: bool = False

    @classmethod
    def acquire(cls, layout: ProjectLayout, service_name: str) -> "ProjectProcessLock":
        normalized = str(service_name or "").strip()
        if not normalized or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in normalized):
            raise ValueError("Service lock name must use lowercase letters, digits, underscores, or hyphens")
        lock_path = layout.service_path("locks", f"{normalized}.lock", create_parent=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            cls._lock_nonblocking(descriptor)
        except (BlockingIOError, OSError) as exc:
            os.close(descriptor)
            raise StateConflictError(
                "Another service instance is already active for the selected project",
                service=normalized,
                project=str(layout.project),
                lock_path=str(lock_path),
            ) from exc
        payload = f"pid={os.getpid()}\nservice={normalized}\nproject={layout.project}\n"
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
        return cls(path=str(lock_path), descriptor=descriptor, service_name=normalized)

    def release(self) -> None:
        if self.released:
            return
        SessionLockStore._unlock(self.descriptor)
        os.close(self.descriptor)
        self.released = True

    def __enter__(self) -> "ProjectProcessLock":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

    @staticmethod
    def _lock_nonblocking(descriptor: int) -> None:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)


class SessionLockStore:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout

    @contextmanager
    def acquire(self, session_id: str) -> Iterator[None]:
        session_id = validate_session_id(session_id)
        lock_path = self.layout.service_path("locks", f"{session_id}.lock", create_parent=True)
        key = str(lock_path)
        with _thread_lock(key):
            held = _held_locks()
            current = held.get(key)
            if current is not None:
                descriptor, depth = current
                held[key] = (descriptor, depth + 1)
                try:
                    yield
                finally:
                    held[key] = (descriptor, depth)
                return
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                self._lock(descriptor)
                held[key] = (descriptor, 1)
                yield
            finally:
                held.pop(key, None)
                self._unlock(descriptor)
                os.close(descriptor)

    @contextmanager
    def acquire_nonblocking(self, session_id: str, *, operation: str) -> Iterator[None]:
        """Acquire one cross-process lock immediately or report a conflict."""
        session_id = validate_session_id(session_id)
        lock_path = self.layout.service_path("locks", f"{session_id}.lock", create_parent=True)
        key = str(lock_path)
        thread_lock = _thread_lock(key)
        if not thread_lock.acquire(blocking=False):
            raise StateConflictError(
                f"Another {operation} is already active",
                operation=operation,
                lock_path=str(lock_path),
            )
        descriptor: int | None = None
        try:
            held = _held_locks()
            if key in held:
                raise StateConflictError(
                    f"Another {operation} is already active",
                    operation=operation,
                    lock_path=str(lock_path),
                )
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                ProjectProcessLock._lock_nonblocking(descriptor)
            except (BlockingIOError, OSError) as exc:
                raise StateConflictError(
                    f"Another {operation} is already active",
                    operation=operation,
                    lock_path=str(lock_path),
                ) from exc
            held[key] = (descriptor, 1)
            yield
        finally:
            _held_locks().pop(key, None)
            if descriptor is not None:
                self._unlock(descriptor)
                os.close(descriptor)
            thread_lock.release()

    @staticmethod
    def _lock(descriptor: int) -> None:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except ImportError:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
