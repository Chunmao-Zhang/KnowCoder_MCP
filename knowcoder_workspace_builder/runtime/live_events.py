"""Live model-event transport outside the protected Harness implementation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


WORKER_EVENT_PREFIX = "@@schema-live-event@@"
BUILDER_EVENT_LOG_ENV = "SCHEMA_BUILDER_LIVE_EVENT_LOG"
STREAM_TEXT_LIMIT = 6_000

LiveEvent = dict[str, Any]
LiveEventSink = Callable[[LiveEvent], None]


def _tail(value: str, limit: int = STREAM_TEXT_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


def message_text(message: Any) -> str:
    content = getattr(message, "content", "") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or part.get("content") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def message_reasoning(message: Any) -> str:
    additional = getattr(message, "additional_kwargs", None) or {}
    if not isinstance(additional, dict):
        return ""
    return str(additional.get("reasoning_content") or "")


def message_stream_id(agent_name: str, message: Any, metadata: dict[str, Any] | None = None) -> str:
    explicit = str((metadata or {}).get("stream_id") or "").strip()
    if explicit:
        return explicit
    message_id = str(getattr(message, "id", "") or "").strip()
    return message_id or f"{agent_name or 'agent'}:model"


def emit_worker_event(event: LiveEvent) -> None:
    if not isinstance(event, dict) or not str(event.get("type") or "").strip():
        raise ValueError("Live worker event requires an object with a type")
    line = WORKER_EVENT_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def decode_worker_event(line: str) -> LiveEvent | None:
    if not line.startswith(WORKER_EVENT_PREFIX):
        return None
    payload = json.loads(line[len(WORKER_EVENT_PREFIX) :])
    if not isinstance(payload, dict) or not str(payload.get("type") or "").strip():
        raise ValueError("Live worker event payload is invalid")
    return payload


class WorkerLiveEmitter:
    """Convert accumulated Harness messages into throttled public UI events."""

    def __init__(
        self,
        *,
        stage: str,
        run_agent: str,
        sink: LiveEventSink,
        context: dict[str, Any] | None = None,
        interval_seconds: float = 0.15,
    ) -> None:
        if not stage.strip() or not run_agent.strip():
            raise ValueError("Live model emitter requires stage and run_agent")
        if interval_seconds < 0:
            raise ValueError("Live model emitter interval cannot be negative")
        self.stage = stage
        self.run_agent = run_agent
        self.sink = sink
        self.context = dict(context or {})
        self.interval_seconds = interval_seconds
        self.agent_name = ""
        self.stream_id = ""
        self._message_id = ""
        self.thinking = ""
        self.output = ""
        self.completion_output = ""
        self._last_emit_at = 0.0
        self._last_payload: tuple[str, str, str] | None = None
        self._tool_call_ids: set[str] = set()
        self._tool_names_without_ids: set[str] = set()

    def on_stream_chunk(
        self,
        agent_name: str,
        message: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._observe_tool_calls(agent_name, message)
        if self._update_model(agent_name, message, metadata):
            self._emit_model(force=False)

    def on_message(self, agent_name: str, message: Any) -> None:
        self._observe_tool_calls(agent_name, message)
        self._update_model(agent_name, message, None)
        if self.thinking or self.output:
            self._emit_model(force=True)

    def on_subagent_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "dispatch":
            target = str(data.get("agent_name") or data.get("agent") or "").strip()
            if not target:
                raise ValueError("Subagent dispatch event requires an Agent name")
            self.sink(
                {
                    "type": "dispatch",
                    "agent": target,
                    "coordinator": self.run_agent,
                    "stage": self.stage,
                    "run_agent": self.run_agent,
                    **self.context,
                }
            )
            return
        if event_type != "tool_end":
            return
        tool_name = str(data.get("tool") or data.get("agent") or "tool").strip() or "tool"
        call_id = str(data.get("tool_call_id") or tool_name).strip()
        agent_name = str(data.get("run_agent") or data.get("agent_name") or self.agent_name).strip()
        self._emit_tool(agent_name, tool_name, "done", call_id)

    def snapshot(self) -> LiveEvent:
        return {
            "agent": self.agent_name,
            "stream_id": self.stream_id,
            "thinking": self.thinking,
            "output": self.output,
            "completion_output": self.completion_output,
        }

    def _update_model(
        self,
        agent_name: str,
        message: Any,
        metadata: dict[str, Any] | None,
    ) -> bool:
        message_type = str(getattr(message, "type", "") or "")
        if message_type and message_type not in {"ai", "AIMessageChunk"}:
            return False
        self.agent_name = str(agent_name or "").strip()
        message_id = str(getattr(message, "id", "") or "").strip()
        explicit_stream_id = str((metadata or {}).get("stream_id") or "").strip()
        if explicit_stream_id:
            stream_id = explicit_stream_id
        elif message_id and message_id != self._message_id:
            stream_id = message_id
        else:
            stream_id = self.stream_id or message_stream_id(agent_name, message, metadata)
        if message_id:
            self._message_id = message_id
        changed = False
        if stream_id != self.stream_id:
            self.stream_id = stream_id
            self.thinking = ""
            self.output = ""
            self.completion_output = ""
            changed = True
        thinking = _tail(message_reasoning(message))
        completion_output = message_text(message)
        output = _tail(completion_output)
        if thinking and thinking != self.thinking:
            self.thinking = thinking
            changed = True
        if output and output != self.output:
            self.output = output
            changed = True
        if completion_output:
            self.completion_output = completion_output
        return changed and bool(self.thinking or self.output)

    def _emit_model(self, *, force: bool) -> None:
        payload_key = (self.stream_id, self.thinking, self.output)
        now = time.monotonic()
        if payload_key == self._last_payload:
            return
        if not force and self._last_payload is not None and now - self._last_emit_at < self.interval_seconds:
            return
        self._last_emit_at = now
        self._last_payload = payload_key
        self.sink(
            {
                "type": "stream",
                "agent": self.agent_name,
                "stage": self.stage,
                "run_agent": self.run_agent,
                "stream_id": self.stream_id,
                "thinking": self.thinking,
                "output": self.output,
                **self.context,
            }
        )

    def _observe_tool_calls(self, agent_name: str, message: Any) -> None:
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("name") or "tool").strip() or "tool"
            call_id = str(call.get("id") or "").strip()
            new_tool_call = True
            if call_id:
                if call_id in self._tool_call_ids:
                    new_tool_call = False
                else:
                    self._tool_call_ids.add(call_id)
                    if tool_name in self._tool_names_without_ids:
                        self._tool_names_without_ids.discard(tool_name)
                        new_tool_call = False
            elif tool_name in self._tool_names_without_ids:
                new_tool_call = False
            else:
                self._tool_names_without_ids.add(tool_name)
            if not new_tool_call:
                continue
            self._emit_tool(agent_name, tool_name, "running", call_id or tool_name)

    def _emit_tool(self, agent_name: str, tool_name: str, status: str, call_id: str) -> None:
        message = {
            "role": "event",
            "kind": "tool",
            "content": f"{tool_name} {'completed' if status == 'done' else 'is running'}.",
            "tool": tool_name,
            "tool_call_id": call_id,
            "status": status,
            "stage": self.stage,
            "agent": str(agent_name or "").strip(),
            "run_agent": self.run_agent,
            **self.context,
        }
        self.sink(
            {
                "type": "activity",
                "agent": str(agent_name or "").strip(),
                "run_agent": self.run_agent,
                "message": message,
                **self.context,
            }
        )


def publish_builder_event(event: LiveEvent) -> bool:
    """Append one Builder event when a Solver-hosted live channel is configured."""
    configured = os.environ.get(BUILDER_EVENT_LOG_ENV, "").strip()
    if not configured:
        return False
    from knowcoder_workspace_builder.runtime.session_context import active_session_paths

    target = active_session_paths().assert_writable(configured)
    if not target.parent.is_dir():
        raise FileNotFoundError(f"Builder live-event parent does not exist: {target.parent}")
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    descriptor = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    return True


class EventLogFollower:
    """Forward complete JSONL records until the owning worker finishes."""

    def __init__(self, path: str | Path, sink: LiveEventSink, *, poll_seconds: float = 0.05) -> None:
        self.path = Path(path)
        self.sink = sink
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="builder-live-events", daemon=True)
        self._error: BaseException | None = None

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Builder live-event follower did not stop")
        if self._error is not None:
            raise RuntimeError("Builder live-event follower failed") from self._error

    def _run(self) -> None:
        position = 0
        try:
            while True:
                if self.path.is_file():
                    with self.path.open("r", encoding="utf-8") as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position = handle.tell()
                    for line in chunk.splitlines():
                        if not line.strip():
                            continue
                        payload = json.loads(line)
                        if not isinstance(payload, dict):
                            raise ValueError("Builder live-event record must be an object")
                        self.sink(payload)
                if self._stop.is_set():
                    return
                self._stop.wait(self.poll_seconds)
        except BaseException as exc:  # noqa: BLE001 - surfaced by close at the process boundary.
            self._error = exc


def communicate_worker(
    process: subprocess.Popen[str],
    request: str,
    *,
    timeout_seconds: float,
    on_event: LiveEventSink | None,
    terminate: Callable[[], None],
) -> tuple[str, str]:
    """Drain worker pipes concurrently while forwarding prefixed stderr events."""
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise ValueError("Worker process requires stdin, stdout, and stderr pipes")
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    reader_errors: list[BaseException] = []

    def read_stdout() -> None:
        try:
            stdout_parts.append(process.stdout.read())
        except BaseException as exc:  # noqa: BLE001 - re-raised after the worker is drained.
            reader_errors.append(exc)

    def read_stderr() -> None:
        forwarding_failed = False
        try:
            for line in process.stderr:
                try:
                    event = decode_worker_event(line.rstrip("\n"))
                except BaseException as exc:  # noqa: BLE001 - recorded while the pipe continues draining.
                    reader_errors.append(exc)
                    forwarding_failed = True
                    continue
                if event is None:
                    stderr_parts.append(line)
                elif on_event is not None and not forwarding_failed:
                    try:
                        on_event(event)
                    except BaseException as exc:  # noqa: BLE001 - recorded while the pipe continues draining.
                        reader_errors.append(exc)
                        forwarding_failed = True
        except BaseException as exc:  # noqa: BLE001 - re-raised after the worker is drained.
            reader_errors.append(exc)

    stdout_thread = threading.Thread(target=read_stdout, name="worker-stdout", daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, name="worker-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        process.stdin.write(request)
        process.stdin.close()
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate()
        process.wait(timeout=10)
        raise
    except BaseException:
        if process.poll() is None:
            terminate()
            process.wait(timeout=10)
        raise
    finally:
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        raise RuntimeError("Worker pipe reader did not stop")
    if reader_errors:
        raise RuntimeError("Worker live-event reader failed") from reader_errors[0]
    return "".join(stdout_parts), "".join(stderr_parts)
