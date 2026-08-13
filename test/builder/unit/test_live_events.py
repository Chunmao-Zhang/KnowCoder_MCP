from __future__ import annotations

import json
import subprocess
import sys
import time
from types import SimpleNamespace

from knowcoder_workspace_builder.runtime.live_events import (
    WORKER_EVENT_PREFIX,
    WorkerLiveEmitter,
    communicate_worker,
)


def _message(*, content: str, reasoning: str, message_id: str = "model-1", tool_calls=None):
    return SimpleNamespace(
        content=content,
        additional_kwargs={"reasoning_content": reasoning},
        id=message_id,
        tool_calls=tool_calls or [],
    )


def test_model_emitter_forces_latest_thinking_and_output_after_throttled_chunks() -> None:
    events: list[dict] = []
    emitter = WorkerLiveEmitter(
        stage="evidence",
        run_agent="workspace_builder",
        sink=events.append,
        interval_seconds=60,
    )
    first = _message(content="", reasoning="Inspecting sources")
    latest = _message(content='{"status":"completed"}', reasoning="Inspecting all confirmed sources")

    emitter.on_stream_chunk("evidence_collector", first, {"stream_id": "stream-1"})
    emitter.on_stream_chunk("evidence_collector", latest, {"stream_id": "stream-1"})
    emitter.on_message("evidence_collector", latest)

    streams = [event for event in events if event["type"] == "stream"]
    assert len(streams) == 2
    assert streams[0]["thinking"] == "Inspecting sources"
    assert streams[-1]["thinking"] == "Inspecting all confirmed sources"
    assert streams[-1]["output"] == '{"status":"completed"}'
    assert emitter.snapshot() == {
        "agent": "evidence_collector",
        "stream_id": "stream-1",
        "thinking": "Inspecting all confirmed sources",
        "output": '{"status":"completed"}',
        "completion_output": '{"status":"completed"}',
    }


def test_worker_transport_forwards_event_before_final_stdout() -> None:
    event = {"type": "stream", "stage": "solve", "thinking": "Working", "output": "Partial"}
    script = (
        "import json,sys,time;"
        "sys.stdin.read();"
        f"sys.stderr.write({WORKER_EVENT_PREFIX!r}+json.dumps({event!r})+'\\n');"
        "sys.stderr.flush();"
        "time.sleep(0.05);"
        "sys.stdout.write(json.dumps({'ok':True})+'\\n');"
        "sys.stdout.flush()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    received: list[tuple[float, dict]] = []
    started = time.monotonic()
    stdout, stderr = communicate_worker(
        process,
        "{}",
        timeout_seconds=2,
        on_event=lambda payload: received.append((time.monotonic(), payload)),
        terminate=process.kill,
    )
    finished = time.monotonic()

    assert json.loads(stdout) == {"ok": True}
    assert stderr == ""
    assert received == [(received[0][0], event)]
    assert started <= received[0][0] < finished
