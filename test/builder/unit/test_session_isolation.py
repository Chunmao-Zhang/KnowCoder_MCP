from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import BuilderError, StateConflictError
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.service.stage_runner import StageRunner
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.tombstones import tombstone_path


def _problem_result(stage: str, attempt_id: str, paths=None) -> StageResult:
    artifact = f".knowcoder_workspace/attempts/{attempt_id}/problem_review.json"
    if paths is not None:
        target = paths.attempts / attempt_id / "problem_review.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"ok": True}), encoding="utf-8")
        artifact = paths.relative_to_project(target)
    return StageResult(
        ok=True,
        stage=stage,
        status="completed",
        report=f"Problem clarification {attempt_id} completed.",
        handoff={
            "workspace_action": "new",
            "base_workspace_id": "",
            "question": "Compare records.",
            "scope": {},
            "steps": ["Compare every requested record."],
            "missing_information": [],
        },
        artifacts={"problem_review": artifact},
    )


class ProblemRunner:
    def run(self, *, stage: str, stage_input: dict[str, object], paths, attempt_id: str, on_event=None) -> StageResult:
        del stage_input
        return _problem_result(stage, attempt_id, paths)

    def cancel(self, attempt_id: str) -> bool:
        del attempt_id
        return False


class BarrierProblemRunner(ProblemRunner):
    def __init__(self, parties: int) -> None:
        self.barrier = threading.Barrier(parties)

    def run(self, *, stage: str, stage_input: dict[str, object], paths, attempt_id: str, on_event=None) -> StageResult:
        self.barrier.wait(timeout=5)
        return super().run(
            stage=stage,
            stage_input=stage_input,
            paths=paths,
            attempt_id=attempt_id,
            on_event=on_event,
        )


class BlockingLateRunner(ProblemRunner):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled: list[str] = []

    def run(self, *, stage: str, stage_input: dict[str, object], paths, attempt_id: str, on_event=None) -> StageResult:
        self.started.set()
        assert self.release.wait(timeout=5)
        return _problem_result(stage, attempt_id)

    def cancel(self, attempt_id: str) -> bool:
        self.cancelled.append(attempt_id)
        self.release.set()
        return True


def _problem_input() -> dict[str, Any]:
    return {
        "question": "Compare records.",
        "upload_paths": [],
        "current_date": "2026-07-16",
        "workspace_context": {},
    }


def test_structured_extract_with_no_structured_sources_skips_without_calling_model(runtime_project: Path) -> None:
    """When no structured sources were classified, structured_extract must skip
    deterministically — write an empty structured draft and never invoke the model."""
    layout = ProjectLayout(runtime_project)
    state = BuildStateStore(layout).create("Compare records.", [], session_id="session-struct-skip-1234")

    class RecordingRunner:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, *, stage, stage_input, paths, attempt_id, on_event=None):
            self.calls.append(stage)
            raise AssertionError("model must not be invoked for an empty structured source list")

        def cancel(self, attempt_id: str) -> bool:
            return False

    runner = RecordingRunner()
    stage_runner = StageRunner(layout, runner)
    paths = layout.session(state.session_id, create=True)
    from knowcoder_workspace_builder.workflow.stages import Stage
    # Move the session to the structured_extract stage via the store so _mark_running agrees.
    from knowcoder_workspace_builder.storage.sessions import BuildStateStore as _BSS
    state = _BSS(layout).update(
        state.session_id, state.version, lambda current: (setattr(current, "stage", Stage.STRUCTURED_EXTRACT), current)[-1]
    )
    input_value = {
        "schema_outline": {"entities": []},
        "sources": [],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/x/structured_draft.json",
        "work_dir": "/.knowcoder_workspace/intermediate/sources",
        "batch_path": "/.knowcoder_workspace/intermediate/attempts/x/structured_batches.json",
        "workspace_context": {},
    }
    accepted = stage_runner.run(state, input_value)
    assert runner.calls == []
    assert accepted.extraction.get("structured", {}).get("status") == "skipped"
    assert str(accepted.stage) in {"document", "Stage.DOCUMENT"}
    draft_path = paths.attempts / accepted.accepted_attempts["structured_extract"] / "structured_draft.json"
    assert draft_path.is_file()
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["entities"] == [] and draft["relations"] == []



    layout = ProjectLayout(runtime_project)
    state = BuildStateStore(layout).create("Compare records.", [], session_id="session-race-1234")
    stage_runner = StageRunner(layout, ProblemRunner())
    start = threading.Barrier(2)

    def invoke() -> str:
        start.wait(timeout=5)
        try:
            return stage_runner.run(state, _problem_input()).status
        except StateConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: invoke(), range(2)))

    assert sorted(statuses) == ["conflict", "needs_problem_confirmation"]
    attempt_records = sorted(layout.session(state.session_id).attempts.glob("*.json"))
    assert len(attempt_records) == 2
    persisted_statuses = sorted(json.loads(path.read_text())["status"] for path in attempt_records)
    assert persisted_statuses == ["completed", "failed"]


def test_four_sessions_can_enter_model_execution_concurrently_without_shared_state(runtime_project: Path) -> None:
    runner = BarrierProblemRunner(4)
    service = BuilderService(runtime_project, agent_runner=runner, recover_interrupted=False)

    def start(index: int) -> dict[str, Any]:
        return service.start_workspace_build(
            question=f"Compare record set {index}.",
            workspace_id=f"session-parallel-{index:04d}",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(start, range(4)))

    assert all(result["status"] == "needs_problem_confirmation" for result in results)
    roots = [service.layout.session(result["session_id"]).root for result in results]
    assert len(set(roots)) == 4
    for result, root in zip(results, roots):
        state = BuildStateStore(service.layout).load(result["session_id"])
        assert state.question.endswith(f"{int(result['session_id'][-4:])}.")
        assert root.parent.name == "sessions"


def test_cancelled_late_result_cannot_replace_newer_state(runtime_project: Path) -> None:
    runner = BlockingLateRunner()
    service = BuilderService(runtime_project, agent_runner=runner, recover_interrupted=False)
    outcome: dict[str, Any] = {}

    def start() -> None:
        outcome.update(
            service.start_workspace_build(
                question="Compare records.",
                workspace_id="session-cancel-1234",
            )
        )

    thread = threading.Thread(target=start)
    thread.start()
    assert runner.started.wait(timeout=5)
    running = BuildStateStore(service.layout).load("session-cancel-1234")
    cancelled = service.cancel_workspace_build(
        workspace_id=running.session_id,
        expected_version=running.version,
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert cancelled["status"] == "cancelled"
    assert outcome["status"] == "cancelled"
    state = BuildStateStore(service.layout).load(running.session_id)
    assert state.problem is None
    assert state.status == "cancelled"
    cards = service.get_workspace_events(workspace_id=running.session_id)["events"]
    terminal_invocations = [event for event in cards if event["kind"] == "invocation" and event["status"] != "running"]
    assert terminal_invocations
    assert all(event["status"] == "cancelled" for event in terminal_invocations)


def test_delete_tombstone_prevents_a_stale_task_from_recreating_session(runtime_project: Path) -> None:
    runner = BlockingLateRunner()
    service = BuilderService(runtime_project, agent_runner=runner, recover_interrupted=False)
    errors: list[Exception] = []

    def start() -> None:
        try:
            service.start_workspace_build(
                question="Compare records.",
                workspace_id="session-delete-1234",
            )
        except Exception as exc:  # noqa: BLE001 - test captures the deleted caller.
            errors.append(exc)

    thread = threading.Thread(target=start)
    thread.start()
    assert runner.started.wait(timeout=5)
    deleted = service.delete_workspace_build(workspace_id="session-delete-1234")
    thread.join(timeout=5)

    assert deleted["status"] == "deleted"
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], BuilderError)
    session_root = service.layout.data_root / "sessions" / "session-delete-1234"
    assert not session_root.exists()
    assert tombstone_path(service.layout.data_root, "session-delete-1234").is_file()
    with pytest.raises(BuilderError, match="cannot be recreated"):
        service.layout.session("session-delete-1234", create=True)


def test_service_restart_marks_interrupted_attempt_failed_and_retry_is_explicit(runtime_project: Path, monkeypatch) -> None:
    # Simulate a genuinely-dead build: disable the liveness window so recovery reaps the
    # session even though its files were just written (no live process owns it in this test).
    monkeypatch.setattr(
        "knowcoder_workspace_builder.service.commands.RECOVERY_LIVENESS_SECONDS", 0.0
    )
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    state = states.create("Compare records.", [], session_id="session-restart-1234")
    attempt = AttemptStore(layout).start(state.session_id, "problem", 1)
    running = states.update(
        state.session_id,
        state.version,
        lambda current: _set_active(current, attempt["attempt_id"]),
    )

    service = BuilderService(runtime_project, agent_runner=ProblemRunner(), recover_interrupted=True)
    recovered = service.get_workspace_status(workspace_id=running.session_id)
    assert recovered["status"] == "failed"
    assert recovered["errors"][0]["code"] == "service_restart"
    assert recovered["metadata"]["next_tool"] == "retry_workspace_build"

    retried = service.retry_workspace_build(
        workspace_id=running.session_id,
        reason="The service was restarted and the previous attempt is no longer active.",
        expected_version=recovered["version"],
    )
    assert retried["status"] == "running"
    assert retried["next_action"] == "resume_builder"
    assert retried["metadata"]["next_tool"] == "resume_workspace_build"


def _set_active(state, attempt_id: str):
    state.active_attempt_id = attempt_id
    state.status = "running"
    state.stage_attempts["problem"] = 1
    return state
