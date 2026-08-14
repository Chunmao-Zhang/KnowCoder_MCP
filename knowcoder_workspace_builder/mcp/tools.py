"""Six framework-neutral MCP tools for the KnowCoder task lifecycle."""

from __future__ import annotations

import threading
import time
from typing import Annotated, Any, Literal
from uuid import uuid4

from mcp.server.fastmcp import Context
from mcp.types import CallToolResult, ResourceLink, TextContent
from pydantic import Field

from knowcoder_workspace_builder.contracts.errors import (
    ContractError,
    StateConflictError,
)
from knowcoder_workspace_builder.review.page import write_review_page
from knowcoder_workspace_builder.review.service import ReviewService
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.storage.project import SelectedProject

from .background import (
    background_job_failure,
    launch_background_resume,
    request_background_cancel,
)
from .schemas import normalize_upload_paths, public_error
from .task_store import TaskRecord, TaskStore

_SERVICE: BuilderService | None = None
_SERVICE_LOCK = threading.Lock()
_REQUEST_SCOPE = uuid4().hex
_WAIT_POLL_SECONDS = 0.25
_REVIEW_STATUSES = {
    "needs_problem_confirmation": "problem",
    "needs_schema_confirmation": "schema",
}
_STAGE_LABELS = {
    "problem": "Analyzing the question and research plan",
    "evidence": "Collecting and organizing evidence",
    "schema_build": "Constructing the knowledge Schema",
    "schema_judge": "Reviewing the knowledge Schema",
    "extract": "Extracting entities and relations from source chunks",
    "structured_extract": "Converting structured files into knowledge records",
    "document": "Preparing the Workspace documentation",
    "ready": "Preparing the completed Workspace",
}


def builder_service() -> BuilderService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BuilderService(SelectedProject.resolve(), recover_interrupted=False)
        return _SERVICE


def task_store() -> TaskStore:
    return TaskStore(builder_service().layout)


def _builder_status(record: TaskRecord) -> dict[str, Any]:
    service = builder_service()
    result = service.get_workspace_status(workspace_id=record.workspace_id)
    if result.get("status") != "running":
        return result
    failure = background_job_failure(
        service,
        record.workspace_id,
        current_version=int(result["version"]),
    )
    if failure is None:
        return result
    return service.fail_background_job(
        workspace_id=record.workspace_id,
        expected_version=int(result["version"]),
        failure=failure,
    )


def _task_result(
    record: TaskRecord,
    token: str,
    builder: dict[str, Any],
    *,
    event: str,
    timed_out: bool = False,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any] | CallToolResult:
    status = record.status
    review_type = _REVIEW_STATUSES.get(str(builder.get("status") or ""))
    if review_type:
        return _review_result(record, token, builder, review_type)
    if status == "running":
        stage_label = _STAGE_LABELS.get(record.stage, record.stage.replace("_", " ").title())
        result = {
            "ok": True,
            "status": status,
            "stage": record.stage,
            "version": record.version,
            "continuation_token": token,
            "event": event,
            "message": f"KnowCoder is working on: {stage_label}.",
            "next_action": "wait",
            "timed_out": timed_out,
        }
        if progress:
            result["progress"] = progress
        return result
    if status == "completed":
        metadata = dict(builder.get("metadata") or {})
        handoff = dict(metadata.get("workspace_handoff") or {})
        return {
            "ok": True,
            "status": status,
            "stage": record.stage,
            "version": record.version,
            "continuation_token": token,
            "event": "task_completed",
            "message": "The Workspace build is complete and ready to read.",
            "next_action": "read_result",
            "result": {
                "workspace_id": record.workspace_id,
                "readme": handoff.get("readme", "README.md"),
                "summary": builder.get("message"),
            },
        }
    errors = list(builder.get("errors") or [])
    error = errors[0] if errors else {"code": "task_failed", "message": builder.get("message")}
    retryable = status == "failed"
    return {
        "ok": False,
        "status": status,
        "stage": record.stage,
        "version": record.version,
        "continuation_token": token,
        "event": "task_failed" if status == "failed" else "task_stopped",
        "message": str(error.get("message") or "The task did not complete."),
        "next_action": "retry" if retryable else "stop",
        "error": {**error, "stage": record.stage, "retryable": retryable},
    }


def _review_result(
    record: TaskRecord,
    token: str,
    builder: dict[str, Any],
    review_type: str,
) -> CallToolResult:
    service = builder_service()
    review_snapshot = ReviewService(service.layout).get(record.workspace_id, review_type)
    page = write_review_page(
        service.layout,
        record.workspace_id,
        review_type,
        record.version,
        review_snapshot,
    )
    uri = page.resolve(strict=True).as_uri()
    workspace_path = service.layout.session(record.workspace_id).relative_to_project(page)
    label = "Problem" if review_type == "problem" else "Schema"
    structured = {
        "ok": True,
        "status": "waiting",
        "stage": record.stage,
        "version": record.version,
        "continuation_token": token,
        "event": "review_required",
        "message": f"The {label} Review is ready. Present it to the user and end this turn.",
        "next_action": "present_review",
        "review": {
            "type": review_type,
            "content": review_snapshot,
            "uri": uri,
            "workspace_path": workspace_path,
        },
    }
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"{label} Review is ready. Show the review content and link to the user, then end this turn. "
                    "The saved page is read-only and remains available with the Workspace. "
                    "The user confirms or requests changes in the host conversation.\n\n"
                    f"[Open {label} Review]({uri})"
                ),
            ),
            ResourceLink(
                type="resource_link",
                name=f"{label} Review",
                title=f"Open {label} Review",
                uri=uri,
                description=f"Open the saved, read-only {label.lower()} review in a browser.",
                mimeType="text/html",
            ),
        ],
        structuredContent=structured,
    )


def _failure(exc: Exception, *, operation: str) -> dict[str, Any]:
    result = public_error(exc)
    error = dict(result.get("error") or {})
    context = dict(error.get("context") or {})
    context.setdefault("operation", operation)
    error["context"] = context
    error.setdefault("retryable", bool(result.get("retryable", False)))
    result["error"] = error
    result["message"] = f"{operation} failed: {error.get('message', str(exc))}"
    return result


def _task_progress(builder: dict[str, Any]) -> dict[str, Any]:
    metadata = builder.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    raw = metadata.get("progress")
    if not isinstance(raw, dict):
        return {}
    completed = raw.get("completed")
    total = raw.get("total")
    if not isinstance(completed, int) or isinstance(completed, bool) or completed < 0:
        return {}
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0 or completed > total:
        return {}
    return {"completed": completed, "total": total}


def start_workspace_task(
    question: Annotated[
        str,
        Field(description="The complete research request. Leave empty only when recovering a failed task."),
    ] = "",
    workspace_id: Annotated[
        str,
        Field(description="An existing Workspace ID for an incremental update. Omit for a new Workspace."),
    ] = "",
    continuation_token: Annotated[
        str,
        Field(description="The failed task token to recover. Do not combine it with question or workspace_id."),
    ] = "",
    upload_paths: list[str] | str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any] | CallToolResult:
    """Purpose: Start, extend, or recover one durable research Workspace.

    Use: Call once when a deep-research Workspace is needed.
    Inputs: Supply question; add workspace_id for extension; use only continuation_token for recovery.
    Returns: Current task status and an opaque continuation_token.
    Next: Call wait_for_task_update while status is running.
    Errors: Fix invalid input; recover only a task whose status is failed.
    """
    try:
        question = str(question or "").strip()
        workspace_id = str(workspace_id or "").strip()
        continuation_token = str(continuation_token or "").strip()
        uploads = normalize_upload_paths(upload_paths)
        service = builder_service()
        store = task_store()
        request_id = str(ctx.request_id or "").strip() if ctx is not None else ""
        request_key = f"{_REQUEST_SCOPE}:{request_id}" if request_id else ""

        existing_request = store.find_request(request_key)
        if existing_request is not None:
            existing_record, existing_token = existing_request
            existing_builder = _builder_status(existing_record)
            return _task_result(
                store.sync(existing_record.task_id, existing_builder),
                existing_token,
                existing_builder,
                event="task_already_started",
            )

        if continuation_token:
            if question or workspace_id or uploads:
                raise ContractError("Task recovery accepts only continuation_token")
            record, token = store.by_token(continuation_token)
            current = _builder_status(record)
            if current.get("status") != "failed":
                raise StateConflictError(
                    "Only a failed task can be recovered",
                    workspace_id=record.workspace_id,
                    status=current.get("status"),
                )
            resumed = service.retry_workspace_build(
                workspace_id=record.workspace_id,
                reason="The user requested recovery after correcting the reported failure.",
                expected_version=int(current["version"]),
            )
            store.sync(record.task_id, resumed)
            store.bind_request(record.task_id, request_key)
            kwargs = {"workspace_id": record.workspace_id, "expected_version": resumed["version"]}
            launch_background_resume(service, record.workspace_id, kwargs, task_id=record.task_id)
            return _task_result(store.sync(record.task_id, resumed), token, resumed, event="task_recovered")

        if not question:
            raise ContractError("A complete research question is required")
        if workspace_id:
            current = service.get_workspace_status(workspace_id=workspace_id)
            if current.get("status") != "workspace_ready":
                raise StateConflictError(
                    "Incremental research requires a completed Workspace",
                    workspace_id=workspace_id,
                    status=current.get("status"),
                )
            record, token = store.create(workspace_id, stage="problem", version=int(current["version"]))
            store.bind_request(record.task_id, request_key)
            advanced = service.resume_workspace_build(
                workspace_id=workspace_id,
                follow_up_request=question,
                change_impacts=["problem"],
                upload_paths=uploads,
                expected_version=int(current["version"]),
            )
            record = store.sync(record.task_id, advanced)
            kwargs = {"workspace_id": workspace_id, "expected_version": advanced["version"]}
        else:
            prepared = service.prepare_workspace_build(question=question, upload_paths=uploads)
            workspace_id = str(prepared["workspace_id"])
            record, token = store.create(workspace_id, stage=str(prepared["stage"]), version=int(prepared["version"]))
            store.bind_request(record.task_id, request_key)
            kwargs = {"workspace_id": workspace_id, "expected_version": prepared["version"]}
            advanced = prepared

        launch_background_resume(service, workspace_id, kwargs, task_id=record.task_id)
        return _task_result(record, token, advanced, event="task_started")
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation="Starting the Workspace task")


def wait_for_task_update(
    continuation_token: str,
    timeout_seconds: Annotated[
        int,
        Field(
            ge=1,
            le=55,
            description="Seconds to wait for one update. Agent callers must use 40 seconds or less.",
        ),
    ] = 40,
) -> dict[str, Any] | CallToolResult:
    """Purpose: Wait once for a background task state or stage change.

    Use: Call serially while the task status is running.
    Inputs: Pass the unchanged continuation_token and set timeout_seconds to 40 or less.
    Returns: Stage progress, a Review, completion, failure, or a no-change timeout.
    Next: Wait again only after this call returns and status remains running. Keep same-stage waits and no-change timeouts silent. When event is stage_started, first send one short progress sentence naming that stage, then continue waiting in the same turn. Present a Review, completion, or failure immediately.
    Errors: Stop waiting and report any token, state, or concurrent-wait error.
    """
    try:
        store = task_store()
        record, token = store.by_token(continuation_token)
        with store.wait_lock(record.task_id):
            initial_stage = record.stage
            initial_version = record.version
            deadline = time.monotonic() + timeout_seconds
            while True:
                builder = _builder_status(record)
                record = store.sync(record.task_id, builder)
                changed = record.version != initial_version or record.stage != initial_stage or record.status != "running"
                if changed:
                    event = "stage_started" if record.status == "running" else "state_changed"
                    return _task_result(record, token, builder, event=event, progress=_task_progress(builder))
                if time.monotonic() >= deadline:
                    return _task_result(
                        record,
                        token,
                        builder,
                        event="no_change",
                        timed_out=True,
                        progress=_task_progress(builder),
                    )
                time.sleep(min(_WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation="Waiting for the Workspace task")


def submit_review_decision(
    continuation_token: str,
    action: Literal["confirm", "revise"],
    expected_version: Annotated[int, Field(ge=1)],
    instruction: str = "",
) -> dict[str, Any] | CallToolResult:
    """Purpose: Submit one user decision for the current Problem or Schema Review.

    Use: Call only when the current user message explicitly confirms the displayed Review or requests a natural-language revision. Never treat the original research request, a waiting status, a Review link, a tool error, or the Agent's own judgement as approval. Never call this tool in the same assistant turn that first presents the Review.
    Inputs: Pass continuation_token, expected_version from the displayed Review, and action="confirm" or action="revise". Pass instruction only with action="revise".
    Returns: The resumed task status and unchanged continuation token.
    Next: Call wait_for_task_update when the resumed task is running.
    Errors: Present stale-version or invalid-decision details to the user.
    """
    try:
        store = task_store()
        record, token = store.by_token(continuation_token)
        builder = _builder_status(record)
        review_type = _REVIEW_STATUSES.get(str(builder.get("status") or ""))
        if review_type is None:
            raise StateConflictError(
                "This task is not waiting for a Problem or Schema Review",
                workspace_id=record.workspace_id,
                status=builder.get("status"),
            )
        if int(builder.get("version") or 0) != expected_version:
            raise StateConflictError(
                "The Review changed after it was shown",
                workspace_id=record.workspace_id,
                expected_version=expected_version,
                current_version=builder.get("version"),
            )
        instruction = str(instruction or "").strip()
        if action == "confirm" and instruction:
            raise ContractError("Confirm does not accept a revision instruction")
        if action == "revise" and not instruction:
            raise ContractError("Revise requires the user's complete change request")
        kwargs = {
            "workspace_id": record.workspace_id,
            "confirmation_type": review_type,
            "user_confirmed": action == "confirm",
            "user_instruction": instruction,
            "expected_version": expected_version,
        }
        advanced = builder_service().resume_workspace_build(**kwargs)
        record = store.sync(record.task_id, advanced)
        kwargs["expected_version"] = advanced["version"]
        launch_background_resume(builder_service(), record.workspace_id, kwargs, task_id=record.task_id)
        return _task_result(record, token, advanced, event="review_submitted")
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation="Submitting the Review decision")


def read_workspace(
    workspace_id: str,
    resource: Literal["readme", "schema", "entities", "relations", "manifest", "source"] = "readme",
    source_path: str = "",
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1, le=100_000)] = 20_000,
) -> dict[str, Any]:
    """Purpose: Read one resource from the accepted Workspace version.

    Use: Start with readme, then read only resources needed for the answer.
    Inputs: Pass workspace_id, resource, and source_path only for a manifest source.
    Returns: One bounded content page and next_offset when more content exists.
    Next: Continue pagination or answer from the resources already read.
    Errors: Correct the resource, source path, or page bounds reported by the tool.
    """
    try:
        artifact = {
            "readme": "workspace_readme",
            "schema": "schema_contract",
            "entities": "entities",
            "relations": "relations",
            "manifest": "manifest",
            "source": source_path,
        }[resource]
        if resource == "source" and not source_path.strip():
            raise ContractError("Reading a source requires source_path from the Workspace manifest")
        result = builder_service().read_workspace_artifact(
            workspace_id=workspace_id,
            artifact=artifact,
            offset=offset,
            max_chars=max_chars,
        )
        return {
            "ok": True,
            "status": "completed",
            "workspace_id": workspace_id,
            "resource": resource,
            "content": result["content"],
            "offset": result["offset"],
            "total_chars": result["total_chars"],
            "next_offset": result["next_offset"],
        }
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation=f"Reading Workspace {resource}")


def find_workspace_tasks(limit: Annotated[int, Field(ge=1, le=50)] = 20) -> dict[str, Any]:
    """Purpose: Find exact tasks and Workspaces after host context is lost.

    Use: Call only when the token or Workspace ID is unavailable.
    Inputs: Supply the maximum number of recent records.
    Returns: Task status, recovery token, question summary, and Workspace records.
    Next: Let the user select when several records could match.
    Errors: Report storage or state errors without selecting a record automatically.
    """
    try:
        store = task_store()
        task_records = store.list_records()[:limit]
        workspaces = builder_service().list_workspaces(limit=limit).get("workspaces", [])
        workspace_by_id = {
            str(item.get("workspace_id") or item.get("session_id") or ""): item
            for item in workspaces
            if isinstance(item, dict)
        }
        tasks = []
        for record in task_records:
            workspace = workspace_by_id.get(record.workspace_id, {})
            tasks.append(
                {
                    **record.to_dict(),
                    "question": str(workspace.get("question") or ""),
                    "continuation_token": store.token_for(record.task_id),
                }
            )
        return {
            "ok": True,
            "status": "completed",
            "tasks": tasks,
            "workspaces": workspaces,
            "message": "Select the exact task or Workspace when more than one item matches.",
        }
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation="Finding Workspace tasks")


def stop_task(continuation_token: str) -> dict[str, Any]:
    """Purpose: Stop one task that is actively consuming background resources.

    Use: Call when the user explicitly stops a running task.
    Inputs: Pass the task continuation_token.
    Returns: Stopped status while preserving accepted Workspace files.
    Next: Start a new task later if the user wants to continue the work.
    Errors: A waiting or terminal task has no active Worker to stop.
    """
    try:
        store = task_store()
        record, token = store.by_token(continuation_token)
        builder = _builder_status(record)
        if record.status == "waiting":
            raise StateConflictError("A task waiting for user review has no active worker to stop")
        if record.status != "running":
            raise StateConflictError("Only a running task can be stopped", status=record.status)
        service = builder_service()
        if request_background_cancel(service, record.workspace_id):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                builder = _builder_status(record)
                if builder.get("status") != "running":
                    break
                time.sleep(_WAIT_POLL_SECONDS)
        if builder.get("status") == "running":
            builder = service.cancel_workspace_build(
                workspace_id=record.workspace_id,
                expected_version=int(builder["version"]),
                reason="The user stopped the active KnowCoder task.",
            )
        record = store.sync(record.task_id, builder)
        return _task_result(record, token, builder, event="task_stopped")
    except Exception as exc:  # noqa: BLE001 - normalized at the MCP boundary.
        return _failure(exc, operation="Stopping the Workspace task")


MCP_TOOL_HANDLERS = (
    start_workspace_task,
    wait_for_task_update,
    submit_review_decision,
    read_workspace,
    find_workspace_tasks,
    stop_task,
)
