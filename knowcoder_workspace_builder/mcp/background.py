"""Detached Builder jobs for MCP hosts with short tool-call lifetimes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from knowcoder_workspace_builder.contracts.errors import StateConflictError
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.project import SelectedProject
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json

from .schemas import public_error
from .task_store import TaskStore


JOB_FILENAME = "mcp_background_job.json"
HEARTBEAT_SECONDS = 5.0
STALE_SECONDS = 30.0
HANDOFF_WAIT_SECONDS = 5.0
ACTIVE_STATUSES = frozenset({"starting", "running"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(service: BuilderService, workspace_id: str) -> Path:
    return service.layout.session(workspace_id).intermediate / JOB_FILENAME


def read_background_job(service: BuilderService, workspace_id: str) -> dict[str, Any] | None:
    path = _job_path(service, workspace_id)
    if not path.is_file():
        return None
    value = read_json(path)
    if not isinstance(value, dict):
        raise StateConflictError("Background Builder job record is invalid", workspace_id=workspace_id)
    return value


def _heartbeat_age(job: dict[str, Any]) -> float:
    try:
        return max(0.0, time.time() - float(job["heartbeat_epoch"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise StateConflictError("Background Builder job has no valid heartbeat") from exc


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def background_job_active(service: BuilderService, workspace_id: str) -> bool:
    job = read_background_job(service, workspace_id)
    if job is None or job.get("status") not in ACTIVE_STATUSES:
        return False
    age = _heartbeat_age(job)
    if age > STALE_SECONDS:
        return _process_is_alive(job.get("pid"))
    return True


def background_job_failure(
    service: BuilderService,
    workspace_id: str,
    *,
    current_version: int,
) -> dict[str, Any] | None:
    """Describe a dead detached job while the Builder still claims to be running."""
    job = read_background_job(service, workspace_id)
    if job is None:
        return None
    status = str(job.get("status") or "")
    result_version = job.get("result_version")
    if (
        status not in ACTIVE_STATUSES
        and isinstance(result_version, int)
        and not isinstance(result_version, bool)
        and result_version < current_version
    ):
        return None
    if status == "failed":
        public = job.get("error")
        public_error_value = public.get("error") if isinstance(public, dict) else None
        cause = public_error_value if isinstance(public_error_value, dict) else {}
        return {
            "code": "background_worker_failed",
            "message": str(cause.get("message") or "The background Builder process failed unexpectedly."),
            "context": {
                "job_id": str(job.get("job_id") or ""),
                "cause_code": str(cause.get("code") or "system_error"),
            },
        }
    if status == "completed":
        return {
            "code": "background_worker_incomplete",
            "message": "The background Builder process stopped before reaching a Review or completed Workspace.",
            "context": {"job_id": str(job.get("job_id") or "")},
        }
    if status not in ACTIVE_STATUSES or _heartbeat_age(job) <= STALE_SECONDS:
        return None
    if _process_is_alive(job.get("pid")):
        return None
    return {
        "code": "background_worker_stopped",
        "message": "The background Builder process stopped responding and is no longer running.",
        "context": {
            "job_id": str(job.get("job_id") or ""),
            "heartbeat_age_seconds": round(_heartbeat_age(job), 3),
        },
    }


def _expected_version(job: dict[str, Any]) -> int | None:
    kwargs = job.get("kwargs")
    value = kwargs.get("expected_version") if isinstance(kwargs, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _wait_for_previous_phase(
    service: BuilderService,
    workspace_id: str,
    *,
    next_expected_version: int,
) -> None:
    """Let a just-finished phase publish its terminal job record before replacement."""
    deadline = time.monotonic() + HANDOFF_WAIT_SECONDS
    while True:
        current = read_background_job(service, workspace_id)
        if current is None or current.get("status") not in ACTIVE_STATUSES:
            return
        previous_version = _expected_version(current)
        if previous_version is None or previous_version >= next_expected_version:
            raise StateConflictError(
                "A background Builder phase is already active for this Workspace",
                workspace_id=workspace_id,
                job_id=current.get("job_id"),
            )
        if time.monotonic() >= deadline:
            raise StateConflictError(
                "The previous background Builder phase did not finish its handoff in time",
                workspace_id=workspace_id,
                job_id=current.get("job_id"),
                wait_seconds=HANDOFF_WAIT_SECONDS,
            )
        time.sleep(0.05)


def launch_background_resume(
    service: BuilderService,
    workspace_id: str,
    kwargs: dict[str, Any],
    *,
    task_id: str,
) -> bool:
    paths = service.layout.session(workspace_id)
    locks = SessionLockStore(service.layout)
    with locks.acquire(workspace_id):
        expected_version = kwargs.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise StateConflictError(
                "Background Builder launch requires the current expected version",
                workspace_id=workspace_id,
            )
        _wait_for_previous_phase(
            service,
            workspace_id,
            next_expected_version=expected_version,
        )
        job_id = uuid4().hex
        job = {
            "format_version": 1,
            "job_id": job_id,
            "workspace_id": workspace_id,
            "task_id": task_id,
            "status": "starting",
            "created_at": _now_iso(),
            "heartbeat_at": _now_iso(),
            "heartbeat_epoch": time.time(),
            "kwargs": kwargs,
        }
        confirmation_type = str(kwargs.get("confirmation_type") or "").strip()
        if confirmation_type and kwargs.get("user_confirmed") is True:
            job["confirmation_notice"] = {
                "confirmation_type": confirmation_type,
                "version": kwargs.get("expected_version"),
            }
        AtomicWriter(paths).json(paths.intermediate / JOB_FILENAME, job)
        environment = {
            **os.environ,
            "SCHEMA_WORKSPACE_PROJECT": service.target_project_dir,
            "SCHEMA_WORKSPACE_RECOVER_INTERRUPTED": "0",
        }
        log_path = paths.intermediate / "mcp_background_worker.log"
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "knowcoder_workspace_builder.mcp.background_worker",
                    workspace_id,
                    job_id,
                ],
                cwd=service.target_project_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        return True


def request_background_cancel(service: BuilderService, workspace_id: str) -> bool:
    job = read_background_job(service, workspace_id)
    if job is None or job.get("status") not in ACTIVE_STATUSES:
        return False
    if not background_job_active(service, workspace_id):
        return False
    pid = job.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise StateConflictError(
            "Background Builder job has not published a process ID",
            workspace_id=workspace_id,
            job_id=job.get("job_id"),
        )
    os.kill(pid, signal.SIGTERM)
    return True


def run_background_worker(workspace_id: str, job_id: str) -> int:
    selected = SelectedProject.resolve()
    service = BuilderService(selected, recover_interrupted=False)
    paths = service.layout.session(workspace_id)
    writer = AtomicWriter(paths)
    job_path = paths.intermediate / JOB_FILENAME
    write_guard = threading.Lock()
    stop_heartbeat = threading.Event()
    job = read_background_job(service, workspace_id)
    if job is None or job.get("job_id") != job_id or job.get("status") != "starting":
        raise StateConflictError(
            "Background Builder job identity does not match its launch record",
            workspace_id=workspace_id,
            job_id=job_id,
        )
    kwargs = job.get("kwargs")
    if not isinstance(kwargs, dict):
        raise StateConflictError("Background Builder job arguments are invalid", workspace_id=workspace_id)
    task_id = str(job.get("task_id") or "").strip()
    if not task_id:
        raise StateConflictError("Background Builder job has no task identity", workspace_id=workspace_id)
    tasks = TaskStore(service.layout)

    def write_job(**updates: Any) -> None:
        nonlocal job
        with write_guard:
            current = read_json(job_path)
            if not isinstance(current, dict) or current.get("job_id") != job_id:
                raise StateConflictError("Background Builder job record changed during execution")
            job = {**current, **updates}
            writer.json(job_path, job)

    def heartbeat() -> None:
        while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
            write_job(heartbeat_at=_now_iso(), heartbeat_epoch=time.time())
            tasks.write_lease(task_id, job_id=job_id, pid=os.getpid(), status="running")

    def stop_worker(_signum: int, _frame: object) -> None:
        current = service.get_workspace_status(workspace_id=workspace_id)
        if current.get("status") == "running":
            service.cancel_workspace_build(
                workspace_id=workspace_id,
                expected_version=int(current["version"]),
                reason="Background Builder job was cancelled by the MCP host.",
            )
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    write_job(
        status="running",
        pid=os.getpid(),
        started_at=_now_iso(),
        heartbeat_at=_now_iso(),
        heartbeat_epoch=time.time(),
    )
    tasks.write_lease(task_id, job_id=job_id, pid=os.getpid(), status="running")
    heartbeat_thread = threading.Thread(target=heartbeat, name="mcp-background-heartbeat", daemon=True)
    heartbeat_thread.start()
    exit_code = 0
    try:
        result = service.resume_workspace_build(**kwargs)
        tasks.sync(task_id, result)
        write_job(
            status="completed",
            finished_at=_now_iso(),
            result_status=result.get("status"),
            result_version=result.get("version"),
            kwargs=None,
        )
    except SystemExit as exc:
        exit_code = int(exc.code or 1)
        write_job(status="cancelled", finished_at=_now_iso(), kwargs=None)
        tasks.sync(task_id, service.get_workspace_status(workspace_id=workspace_id))
    except Exception as exc:  # noqa: BLE001 - persisted for the next MCP status query.
        exit_code = 1
        current = service.get_workspace_status(workspace_id=workspace_id)
        write_job(
            status="failed",
            finished_at=_now_iso(),
            result_status=current.get("status"),
            result_version=current.get("version"),
            error=public_error(exc),
            kwargs=None,
        )
        try:
            tasks.sync(task_id, service.get_workspace_status(workspace_id=workspace_id))
        except Exception:
            pass
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=HEARTBEAT_SECONDS + 1)
        tasks.write_lease(task_id, job_id=job_id, pid=os.getpid(), status="stopped")
    return exit_code
