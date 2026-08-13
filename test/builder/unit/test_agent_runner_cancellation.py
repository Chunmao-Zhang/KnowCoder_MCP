from __future__ import annotations

import signal

from knowcoder_workspace_builder.agents.runner import HarnessAgentRunner


class _Process:
    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def test_parent_cancellation_terminates_registered_child_processes(monkeypatch) -> None:
    runner = HarnessAgentRunner(timeout_seconds=10)
    child_process = _Process()
    signals: list[tuple[object, int]] = []
    runner.register_child("parent-attempt", "child-attempt")
    runner._processes["child-attempt"] = child_process
    monkeypatch.setattr(runner, "_signal_process_group", lambda process, number: signals.append((process, number)))

    assert runner.cancel("parent-attempt") is True
    assert signals == [(child_process, signal.SIGTERM)]


def test_child_registered_after_parent_cancellation_starts_cancelled() -> None:
    runner = HarnessAgentRunner(timeout_seconds=10)

    assert runner.cancel("parent-attempt") is False
    runner.register_child("parent-attempt", "late-child-attempt")

    assert "late-child-attempt" in runner._cancelled_attempts
