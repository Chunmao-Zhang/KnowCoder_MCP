"""Subprocess adapter for invoking the protected Builder Harness."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import (
    AgentProtocolError,
    AttemptCancelledError,
    ExternalServiceError,
    InvocationTimeoutError,
    TRANSIENT_EXTERNAL_ERROR_TYPES,
)
from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.live_events import LiveEventSink, communicate_worker
from knowcoder_workspace_builder.runtime.session_context import harness_session_environment
from knowcoder_workspace_builder.runtime.timeouts import agent_attempt_timeout_seconds
from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.validation.inputs import validate_stage_input
from knowcoder_workspace_builder.validation.stage_results import validate_stage_result


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
CANCEL_GRACE_SECONDS = 3.0


class AgentRunner(Protocol):
    def run(
        self,
        *,
        stage: str,
        stage_input: dict[str, object],
        paths: SessionPaths,
        attempt_id: str,
        on_event: LiveEventSink | None = None,
    ) -> StageResult: ...

    def cancel(self, attempt_id: str) -> bool: ...


class HarnessAgentRunner:
    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        if timeout_seconds is None:
            timeout_seconds = agent_attempt_timeout_seconds()
        if timeout_seconds <= 0:
            raise ValueError("Harness timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._child_attempts: dict[str, set[str]] = {}
        self._parent_attempts: dict[str, str] = {}
        self._cancelled_attempts: set[str] = set()
        self._lock = threading.Lock()

    def run(
        self,
        *,
        stage: str,
        stage_input: dict[str, object],
        paths: SessionPaths,
        attempt_id: str,
        on_event: LiveEventSink | None = None,
    ) -> StageResult:
        normalized = validate_stage_input(stage, stage_input)
        write_invocation_context(paths, attempt_id, stage, normalized)
        request = json.dumps({"stage": stage, "input": normalized}, ensure_ascii=False)
        with harness_session_environment(paths, attempt_id) as additions:
            environment = {**os.environ, **additions}
            python_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value for value in (str(PACKAGE_ROOT), python_path) if value
            )
            process = subprocess.Popen(
                [sys.executable, "-m", "knowcoder_workspace_builder.runtime.harness_worker"],
                cwd=PACKAGE_ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                if attempt_id in self._processes:
                    self._signal_process_group(process, signal.SIGKILL)
                    raise AgentProtocolError("Attempt already has a Harness process", attempt_id=attempt_id)
                self._processes[attempt_id] = process
                parent_attempt_id = self._parent_attempts.get(attempt_id, "")
                cancelled = attempt_id in self._cancelled_attempts or parent_attempt_id in self._cancelled_attempts
            try:
                if cancelled:
                    self._signal_process_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=CANCEL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        self._signal_process_group(process, signal.SIGKILL)
                        process.wait()
                    raise AttemptCancelledError("Builder Harness invocation was cancelled", attempt_id=attempt_id)
                stdout, stderr = communicate_worker(
                    process,
                    request,
                    timeout_seconds=self.timeout_seconds,
                    on_event=on_event,
                    terminate=lambda: self._signal_process_group(process, signal.SIGKILL),
                )
            except subprocess.TimeoutExpired as exc:
                raise InvocationTimeoutError(
                    "Builder Harness invocation timed out",
                    attempt_id=attempt_id,
                    timeout_seconds=self.timeout_seconds,
                ) from exc
            except RuntimeError as exc:
                raise AgentProtocolError(
                    "Builder Harness live-event transport failed",
                    attempt_id=attempt_id,
                    error_type=type(exc.__cause__ or exc).__name__,
                ) from exc
            finally:
                with self._lock:
                    self._processes.pop(attempt_id, None)

        if process.returncode in {-15, -9}:
            raise AttemptCancelledError("Builder Harness invocation was cancelled", attempt_id=attempt_id)
        payload = self._worker_payload(stdout, stderr, process.returncode)
        if payload.get("ok") is False and "result" not in payload:
            error_type = str(payload.get("error_type") or "agent_error")
            message = str(payload.get("error") or "Builder Harness failed without an error message")
            if error_type in TRANSIENT_EXTERNAL_ERROR_TYPES or error_type == "ExternalServiceError":
                raise ExternalServiceError(message, error_type=error_type, attempt_id=attempt_id)
            if error_type == "AuthenticationError" or message.startswith("Missing API key for provider"):
                raise ExternalServiceError(message, error_type=error_type, attempt_id=attempt_id)
            raise AgentProtocolError(message, error_type=error_type, attempt_id=attempt_id)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AgentProtocolError("Builder Harness worker returned no stage result", attempt_id=attempt_id)
        return validate_stage_result(result, expected_stage=stage, stage_input=normalized)

    @staticmethod
    def _worker_payload(stdout: str, stderr: str, returncode: int) -> dict[str, object]:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise AgentProtocolError(
                "Builder Harness worker emitted an invalid response",
                returncode=returncode,
                stdout_line_count=len(lines),
                stderr=stderr[-2000:],
            )
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise AgentProtocolError(
                "Builder Harness worker response is not valid JSON",
                returncode=returncode,
                stderr=stderr[-2000:],
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProtocolError("Builder Harness worker response must be an object")
        return payload

    def cancel(self, attempt_id: str) -> bool:
        with self._lock:
            self._cancelled_attempts.add(attempt_id)
            target_ids = {attempt_id, *self._child_attempts.get(attempt_id, set())}
            processes = [
                process
                for target_id in target_ids
                if (process := self._processes.get(target_id)) is not None and process.poll() is None
            ]
        for process in processes:
            self._signal_process_group(process, signal.SIGTERM)
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        for process in processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._signal_process_group(process, signal.SIGKILL)
        return bool(processes)

    def register_child(self, parent_attempt_id: str, child_attempt_id: str) -> None:
        with self._lock:
            self._child_attempts.setdefault(parent_attempt_id, set()).add(child_attempt_id)
            self._parent_attempts[child_attempt_id] = parent_attempt_id
            if parent_attempt_id in self._cancelled_attempts:
                self._cancelled_attempts.add(child_attempt_id)

    def unregister_child(self, parent_attempt_id: str, child_attempt_id: str) -> None:
        with self._lock:
            children = self._child_attempts.get(parent_attempt_id)
            if children is not None:
                children.discard(child_attempt_id)
                if not children:
                    self._child_attempts.pop(parent_attempt_id, None)
            self._parent_attempts.pop(child_attempt_id, None)
            self._cancelled_attempts.discard(child_attempt_id)

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[str], signal_number: int) -> None:
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return
