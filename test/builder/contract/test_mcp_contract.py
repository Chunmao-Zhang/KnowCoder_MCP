from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp.types import CallToolResult

from knowcoder_workspace_builder.contracts.integration import server_instructions
from knowcoder_workspace_builder.mcp import tools as mcp_tools
from knowcoder_workspace_builder.mcp import background as mcp_background
from knowcoder_workspace_builder.mcp.server import create_server
from knowcoder_workspace_builder.mcp.task_store import TaskStore
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore


EXPECTED_TOOLS = {
    "start_workspace_task",
    "wait_for_task_update",
    "submit_review_decision",
    "read_workspace",
    "find_workspace_tasks",
    "stop_task",
}


@pytest.fixture(autouse=True)
def reset_mcp_service() -> None:
    mcp_tools._SERVICE = None
    yield
    mcp_tools._SERVICE = None


def _problem_gate(state):
    state.problem = {
        "question": state.question,
        "scope": {},
        "steps": ["Collect authoritative evidence."],
        "missing_information": [],
    }
    state.status = "needs_problem_confirmation"
    return state


def test_server_instructions_use_the_final_six_tool_lifecycle() -> None:
    instructions = server_instructions()
    for name in EXPECTED_TOOLS:
        assert f"`{name}`" in instructions
    assert "any task that needs deep research" in instructions
    assert "one `wait_for_task_update`" in instructions
    assert "Keep ordinary waits silent" in instructions
    assert "no-change timeout" in instructions
    assert "different stage or Subagent starts" in instructions
    assert "End the current turn" in instructions
    assert "current user message explicitly confirms" in instructions
    assert "Never confirm on the user's behalf" in instructions
    assert "not approval" in instructions
    assert "## Examples" in instructions
    for retired in ("start_workspace_build", "resume_workspace_build", "wait_for_workspace_update"):
        assert retired not in instructions


def test_fastmcp_publishes_exactly_six_simple_tools() -> None:
    tools = asyncio.run(create_server().list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == EXPECTED_TOOLS
    assert "build_mode" not in by_name["start_workspace_task"].inputSchema["properties"]
    assert set(by_name["submit_review_decision"].inputSchema["properties"]) == {
        "continuation_token",
        "action",
        "expected_version",
        "instruction",
    }
    assert by_name["wait_for_task_update"].inputSchema["properties"]["timeout_seconds"]["maximum"] == 50


def test_task_store_keeps_minimal_state_and_opaque_token(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    workspace_id = "workspace-task-store-1234"
    layout.session(workspace_id, create=True)
    store = TaskStore(layout)
    record, token = store.create(workspace_id, stage="problem", version=1)

    assert store.by_token(token)[0] == record
    assert set(record.to_dict()) == {"task_id", "workspace_id", "status", "stage", "version", "updated_at"}
    assert token not in (store._task_root(record.task_id) / "state.json").read_text(encoding="utf-8")

    with pytest.raises(Exception, match="active task"):
        store.create(workspace_id, stage="problem", version=1)


def test_waiting_review_returns_read_only_page_and_stops_waiting(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    initial = states.create("Research one subject.", [], session_id="workspace-review-task-1234")
    gate = states.update(initial.session_id, initial.version, _problem_gate)
    service = BuilderService(runtime_project, recover_interrupted=False)
    monkeypatch.setattr(mcp_tools, "_SERVICE", service)
    store = TaskStore(layout)
    record, token = store.create(gate.session_id, stage="problem", version=gate.version)

    result = mcp_tools.wait_for_task_update(token, timeout_seconds=1)

    assert isinstance(result, CallToolResult)
    structured = dict(result.structuredContent or {})
    assert structured["status"] == "waiting"
    assert structured["next_action"] == "present_review"
    assert structured["review"]["type"] == "problem"
    assert structured["review"]["uri"].startswith("file://")
    assert structured["review"]["workspace_path"].endswith(f"problem-v{gate.version}.html")
    review_file = Path(structured["review"]["uri"].removeprefix("file://"))
    assert review_file.is_file()
    assert structured["continuation_token"] == token
    assert TaskStore(layout).load(record.task_id).status == "waiting"


def test_second_long_wait_is_rejected_while_the_first_wait_is_active(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    layout.session("workspace-concurrent-wait-1234", create=True)
    store = TaskStore(layout)
    _record, token = store.create("workspace-concurrent-wait-1234", stage="problem", version=1)
    first_status_read = threading.Event()
    release_first_wait = threading.Event()

    def running_status(_record):
        first_status_read.set()
        release_first_wait.wait(timeout=2)
        return {"status": "running", "stage": "problem", "version": 1, "metadata": {}}

    monkeypatch.setattr(mcp_tools, "task_store", lambda: store)
    monkeypatch.setattr(mcp_tools, "_builder_status", running_status)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(mcp_tools.wait_for_task_update, token, 1)
        assert first_status_read.wait(timeout=1)
        second = pool.submit(mcp_tools.wait_for_task_update, token, 1).result(timeout=1)
        release_first_wait.set()
        first.result(timeout=2)

    assert second["ok"] is False
    assert second["error"]["code"] == "state_conflict"
    assert "Another wait for this task is already active" in second["message"]


def test_different_tasks_can_wait_at_the_same_time(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    workspace_ids = ["workspace-parallel-wait-1234", "workspace-parallel-wait-5678"]
    for workspace_id in workspace_ids:
        layout.session(workspace_id, create=True)
    store = TaskStore(layout)
    task_tokens = [
        store.create(workspace_id, stage="problem", version=1)[1]
        for workspace_id in workspace_ids
    ]
    both_waiting = threading.Barrier(2)

    def running_status(_record):
        both_waiting.wait(timeout=2)
        return {"status": "running", "stage": "problem", "version": 1, "metadata": {}}

    monkeypatch.setattr(mcp_tools, "task_store", lambda: store)
    monkeypatch.setattr(mcp_tools, "_builder_status", running_status)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda token: mcp_tools.wait_for_task_update(token, 1), task_tokens))

    assert all(result["ok"] is True for result in results)
    assert all(result["event"] == "no_change" for result in results)


def test_wait_reconciles_a_dead_background_worker_as_recoverable_failure(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    state = states.create("Research one subject.", [], session_id="workspace-dead-worker-1234")
    service = BuilderService(runtime_project, recover_interrupted=False)
    monkeypatch.setattr(mcp_tools, "_SERVICE", service)
    store = TaskStore(layout)
    _record, token = store.create(state.session_id, stage="problem", version=state.version)
    job_path = layout.session(state.session_id).intermediate / "mcp_background_job.json"
    job_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "job_id": "dead-job",
                "workspace_id": state.session_id,
                "status": "running",
                "pid": 999_999_999,
                "heartbeat_epoch": time.time() - 60,
            }
        ),
        encoding="utf-8",
    )

    result = mcp_tools.wait_for_task_update(token, timeout_seconds=1)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["next_action"] == "retry"
    assert result["error"]["code"] == "background_worker_stopped"
    assert BuildStateStore(layout).load(state.session_id).status == "failed"


def test_new_background_phase_waits_for_previous_review_phase_handoff(
    runtime_project: Path,
    monkeypatch,
) -> None:
    service = BuilderService(runtime_project, recover_interrupted=False)
    paths = service.layout.session("workspace-phase-handoff-1234", create=True)
    job_path = paths.intermediate / "mcp_background_job.json"
    job_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "job_id": "previous-phase",
                "workspace_id": paths.session_id,
                "task_id": "task-phase-handoff-1234",
                "status": "running",
                "heartbeat_epoch": time.time(),
                "kwargs": {"workspace_id": paths.session_id, "expected_version": 2},
            }
        ),
        encoding="utf-8",
    )
    launched: list[list[str]] = []

    class ProcessStub:
        pass

    monkeypatch.setattr(
        mcp_background.subprocess,
        "Popen",
        lambda args, **_kwargs: launched.append(args) or ProcessStub(),
    )

    def finish_previous() -> None:
        time.sleep(0.1)
        current = json.loads(job_path.read_text(encoding="utf-8"))
        job_path.write_text(json.dumps({**current, "status": "completed", "kwargs": None}), encoding="utf-8")

    thread = threading.Thread(target=finish_previous)
    thread.start()
    result = mcp_background.launch_background_resume(
        service,
        paths.session_id,
        {"workspace_id": paths.session_id, "expected_version": 4},
        task_id="task-phase-handoff-1234",
    )
    thread.join(timeout=1)

    assert result is True
    assert len(launched) == 1
    current = json.loads(job_path.read_text(encoding="utf-8"))
    assert current["status"] == "starting"
    assert current["kwargs"]["expected_version"] == 4


def test_previous_completed_phase_is_not_mistaken_for_current_phase_failure(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    initial = states.create("Research one subject.", [], session_id="workspace-phase-version-1234")
    advanced = states.update(initial.session_id, initial.version, lambda current: current)
    service = BuilderService(runtime_project, recover_interrupted=False)
    monkeypatch.setattr(mcp_tools, "_SERVICE", service)
    store = TaskStore(layout)
    record, _token = store.create(advanced.session_id, stage="evidence", version=advanced.version)
    job_path = layout.session(advanced.session_id).intermediate / "mcp_background_job.json"
    job_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "job_id": "completed-previous-phase",
                "workspace_id": advanced.session_id,
                "status": "completed",
                "result_status": "needs_problem_confirmation",
                "result_version": initial.version,
                "heartbeat_epoch": time.time(),
                "kwargs": None,
            }
        ),
        encoding="utf-8",
    )

    result = mcp_tools._builder_status(record)

    assert result["status"] == "running"
    assert BuildStateStore(layout).load(advanced.session_id).status == "running"


def test_request_deduplication_keys_are_scoped_to_one_mcp_process(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    workspace_ids = ["workspace-request-scope-1234", "workspace-request-scope-5678"]
    for workspace_id in workspace_ids:
        layout.session(workspace_id, create=True)
    store = TaskStore(layout)
    records = [store.create(workspace_id, stage="problem", version=1)[0] for workspace_id in workspace_ids]

    store.bind_request(records[0].task_id, "process-a:request-1")
    store.bind_request(records[1].task_id, "process-b:request-1")

    assert store.find_request("process-a:request-1")[0].task_id == records[0].task_id
    assert store.find_request("process-b:request-1")[0].task_id == records[1].task_id


def test_review_decision_uses_version_and_starts_one_background_phase(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    initial = states.create("Research one subject.", [], session_id="workspace-decision-task-1234")
    gate = states.update(initial.session_id, initial.version, _problem_gate)
    service = BuilderService(runtime_project, recover_interrupted=False)
    monkeypatch.setattr(mcp_tools, "_SERVICE", service)
    store = TaskStore(layout)
    _record, token = store.create(gate.session_id, stage="problem", version=gate.version)
    launches: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(
        mcp_tools,
        "launch_background_resume",
        lambda _service, workspace_id, kwargs, *, task_id: launches.append((workspace_id, kwargs, task_id)) or True,
    )

    result = mcp_tools.submit_review_decision(token, "confirm", gate.version)

    assert result["status"] == "running"
    assert result["next_action"] == "wait"
    assert len(launches) == 1
    assert launches[0][0] == gate.session_id
    assert launches[0][1]["confirmation_type"] == "problem"

    stale = mcp_tools.submit_review_decision(token, "confirm", gate.version)
    assert stale["ok"] is False
    assert "not waiting" in stale["message"]


def test_incremental_start_updates_the_same_workspace_id(runtime_project: Path, monkeypatch) -> None:
    class CompletedService:
        def __init__(self) -> None:
            self.layout = ProjectLayout(runtime_project)
            self.layout.session("workspace-incremental-1234", create=True)

        def get_workspace_status(self, **_kwargs):
            return {"status": "workspace_ready", "stage": "ready", "version": 8}

        def resume_workspace_build(self, **kwargs):
            assert kwargs["workspace_id"] == "workspace-incremental-1234"
            assert kwargs["follow_up_request"] == "Add the missing cost comparison."
            return {"status": "running", "stage": "problem", "version": 9}

    service = CompletedService()
    monkeypatch.setattr(mcp_tools, "builder_service", lambda: service)
    monkeypatch.setattr(mcp_tools, "launch_background_resume", lambda *_args, **_kwargs: True)

    result = mcp_tools.start_workspace_task(
        question="Add the missing cost comparison.",
        workspace_id="workspace-incremental-1234",
    )

    record, _token = TaskStore(service.layout).by_token(result["continuation_token"])
    assert record.workspace_id == "workspace-incremental-1234"
    assert result["status"] == "running"


def test_tool_errors_name_the_failed_operation() -> None:
    result = mcp_tools.start_workspace_task()
    assert result["ok"] is False
    assert result["message"].startswith("Starting the Workspace task failed:")
    assert result["error"]["context"]["operation"] == "Starting the Workspace task"


def test_task_discovery_returns_the_token_needed_to_resume(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    states = BuildStateStore(layout)
    state = states.create("Research one subject.", [], session_id="workspace-discovery-task-1234")
    store = TaskStore(layout)
    record, token = store.create(state.session_id, stage="problem", version=state.version)
    monkeypatch.setattr(mcp_tools, "_SERVICE", BuilderService(runtime_project, recover_interrupted=False))

    result = mcp_tools.find_workspace_tasks()

    found = next(item for item in result["tasks"] if item["task_id"] == record.task_id)
    assert found["continuation_token"] == token
    assert found["question"] == "Research one subject."
