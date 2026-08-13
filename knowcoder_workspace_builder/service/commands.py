"""Start, resume, confirm, revise, retry, cancel, delete, and recover commands."""

from __future__ import annotations

import shutil
import time
from typing import Any

from knowcoder_workspace_builder.agents.runner import AgentRunner
from knowcoder_workspace_builder.contracts.errors import ContractError, MissingStateError, StateConflictError
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.events import EventStore
from knowcoder_workspace_builder.storage.extensions import snapshot_workspace_baseline
from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout, new_session_id, validate_session_id
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.tombstones import mark_deleted
from knowcoder_workspace_builder.storage.uploads import ingest_uploads
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import Stage
from knowcoder_workspace_builder.workflow.transitions import ChangeImpact

from .coordinator import Coordinator
from .follow_up import FollowUpPlan

# A running session whose events/attempt files were touched within this window is treated
# as a live build (owned by another process) and left alone by recover_interrupted.
RECOVERY_LIVENESS_SECONDS = 90.0


class BuildCommands:
    def __init__(self, layout: ProjectLayout, coordinator: Coordinator, agent_runner: AgentRunner) -> None:
        self.layout = layout
        self.coordinator = coordinator
        self.agent_runner = agent_runner
        self.states = BuildStateStore(layout)
        self.attempts = AttemptStore(layout)
        self.events = EventStore(layout)
        self.locks = SessionLockStore(layout)

    def start(
        self,
        *,
        question: str,
        upload_paths: list[str],
        session_id: str | None = None,
        turn_id: str = "",
    ) -> BuildState:
        state = self.prepare_start(
            question=question,
            upload_paths=upload_paths,
            session_id=session_id,
        )
        return self.coordinator.run_until_gate(state, turn_id=turn_id)

    def prepare_start(
        self,
        *,
        question: str,
        upload_paths: list[str],
        session_id: str | None = None,
    ) -> BuildState:
        """Create a Session without running its first long Builder stage."""
        normalized_question = str(question or "").strip()
        if not normalized_question:
            raise ContractError("Question cannot be empty")
        session_id = validate_session_id(session_id) if session_id else new_session_id()
        existing = self.layout.session(session_id)
        if (existing.state / "builder.json").exists():
            raise StateConflictError("Session already exists", session_id=session_id)
        paths = self.layout.session(session_id, create=True)
        try:
            ingested = ingest_uploads(paths, list(upload_paths))
            state = self.states.create(normalized_question, ingested, session_id=session_id)
        except Exception:
            shutil.rmtree(paths.root, ignore_errors=True)
            raise
        return state

    def resume(
        self,
        session_id: str,
        *,
        confirmation_type: str = "",
        user_confirmed: bool = True,
        user_instruction: str = "",
        expected_version: int | None = None,
        turn_id: str = "",
        follow_up_request: str = "",
        change_impacts: list[str] | None = None,
        evidence_step_indexes: list[int] | None = None,
        upload_paths: list[str] | None = None,
    ) -> BuildState:
        state = self.states.load(session_id)
        if expected_version is not None and state.version != expected_version:
            raise StateConflictError(
                "Builder version changed before resume",
                expected_version=expected_version,
                current_version=state.version,
            )
        if state.status.startswith("needs_"):
            return self._handle_confirmation(
                state,
                confirmation_type=confirmation_type,
                user_confirmed=user_confirmed,
                user_instruction=user_instruction,
                turn_id=turn_id,
            )
        if state.status == "running":
            return self.coordinator.run_until_gate(state, turn_id=turn_id)
        if state.status == "workspace_ready":
            return self.follow_up(
                state,
                request=follow_up_request,
                impacts=change_impacts,
                evidence_step_indexes=evidence_step_indexes,
                upload_paths=list(upload_paths or []),
                turn_id=turn_id,
            )
        raise StateConflictError("Builder cannot resume from its current status", status=state.status)

    def follow_up(
        self,
        state: BuildState,
        *,
        request: str,
        impacts: list[str] | None = None,
        evidence_step_indexes: list[int] | None = None,
        upload_paths: list[str] | None = None,
        turn_id: str = "",
    ) -> BuildState:
        normalized_impacts = list(impacts or [])
        if upload_paths:
            for implied in (ChangeImpact.EVIDENCE.value, ChangeImpact.INSTANCES.value):
                if implied not in normalized_impacts:
                    normalized_impacts.append(implied)
        plan = FollowUpPlan.create(
            request,
            normalized_impacts,
            evidence_step_indexes=evidence_step_indexes,
        )
        plan.validate(state)
        if plan.restart_stage is None:
            return state

        def apply_plan(current: BuildState) -> BuildState:
            paths = self.layout.session(current.session_id)
            snapshot_workspace_baseline(self.layout, current.session_id)
            updated = plan.apply(current)
            if upload_paths:
                incoming = [str(path) for path in upload_paths if str(path).strip()]
                ingested = ingest_uploads(paths, incoming)
                updated.upload_paths = list(dict.fromkeys([*updated.upload_paths, *ingested]))
            return updated

        updated = self.states.update(state.session_id, state.version, apply_plan)
        self.events.append(
            state.session_id,
            kind="invalidation",
            status="completed",
            turn_id=turn_id,
            stage=str(plan.restart_stage),
            report=f"Invalidated Builder results from {plan.restart_stage} for the accepted follow-up.",
            public_data={
                "change_impacts": [item.value for item in plan.submitted_impacts],
                "research_scope_change": plan.research_scope_change.value,
                "evidence_step_indexes": list(updated.pending_evidence_step_indexes),
                "restart_stage": str(plan.restart_stage),
                "uploaded_file_count": len(list(upload_paths or [])),
            },
        )
        return updated

    def _handle_confirmation(
        self,
        state: BuildState,
        *,
        confirmation_type: str,
        user_confirmed: bool,
        user_instruction: str,
        turn_id: str,
    ) -> BuildState:
        gate = "problem" if state.status == "needs_problem_confirmation" else "schema"
        supplied_gate = str(confirmation_type or "").strip()
        if supplied_gate and supplied_gate != gate:
            raise StateConflictError("Confirmation type does not match the current gate", expected=gate, actual=supplied_gate)
        instruction = str(user_instruction or "").strip()
        if not user_confirmed and not instruction:
            raise ContractError("A revision request requires user_instruction", confirmation_type=gate)
        if user_confirmed and instruction:
            raise ContractError(
                "A confirmation with requested changes must be submitted as a revision",
                confirmation_type=gate,
            )
        updated = self.states.update(
            state.session_id,
            state.version,
            lambda current: self._apply_confirmation(current, gate, user_confirmed, instruction),
        )
        revalidating = gate == "schema" and user_confirmed and not updated.schema_confirmed
        event_status = "waiting" if revalidating or not user_confirmed else "completed"
        self.events.append(
            state.session_id,
            kind="confirmation",
            status=event_status,
            turn_id=turn_id,
            stage=gate,
            report="User confirmed the review." if user_confirmed else "User requested a revision.",
            public_data={"confirmation_type": gate, "confirmed": user_confirmed},
        )
        if user_confirmed and not revalidating:
            self.events.append(
                state.session_id,
                kind="stage_state",
                status="completed",
                turn_id=turn_id,
                stage=Stage.PROBLEM if gate == "problem" else Stage.SCHEMA_JUDGE,
                report=f"{gate.capitalize()} review confirmed.",
            )
        return updated

    @staticmethod
    def _apply_confirmation(
        current: BuildState,
        gate: str,
        confirmed: bool,
        instruction: str,
    ) -> BuildState:
        if gate == "problem":
            current.problem_confirmed = confirmed
            current.stage = Stage.EVIDENCE if confirmed else Stage.PROBLEM
            if not confirmed and current.invalidated_from == Stage.PROBLEM:
                current.active_follow_up_request = instruction
        else:
            requires_revalidation = bool((current.schema_review or {}).get("requires_revalidation"))
            current.schema_confirmed = confirmed and not requires_revalidation
            current.stage = (
                Stage.SCHEMA_JUDGE
                if confirmed and requires_revalidation
                else Stage.EXTRACT if confirmed else Stage.SCHEMA_BUILD
            )
        current.status = "running"
        current.pending_revision = "" if confirmed else instruction
        current.retry_reason = ""
        current.failure = None
        return current

    def retry(self, session_id: str, *, reason: str, expected_version: int | None = None) -> BuildState:
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ContractError("Retry requires the changed input or external recovery reason")
        state = self.states.load(session_id)
        if expected_version is not None and state.version != expected_version:
            raise StateConflictError(
                "Builder version changed before retry",
                expected_version=expected_version,
                current_version=state.version,
            )
        if state.status not in {"failed", "cancelled"}:
            raise StateConflictError("Only failed or cancelled builds can be retried", status=state.status)
        return self.states.update(
            session_id,
            state.version,
            lambda current: self._mark_retry(current, normalized_reason),
        )

    def fail_background_job(
        self,
        session_id: str,
        *,
        expected_version: int,
        failure: dict[str, Any],
    ) -> BuildState:
        """Close a running state whose detached MCP Worker has terminated."""
        state = self.states.load(session_id)
        if state.version != expected_version:
            raise StateConflictError(
                "Builder version changed before background failure reconciliation",
                expected_version=expected_version,
                current_version=state.version,
            )
        if state.status != "running":
            return state
        code = str(failure.get("code") or "background_worker_failed").strip()
        message = str(failure.get("message") or "The background Builder process failed.").strip()
        context = failure.get("context") if isinstance(failure.get("context"), dict) else {}
        normalized = {"code": code, "message": message, "context": dict(context)}
        attempt_id = state.active_attempt_id
        failed = self.states.update(
            session_id,
            state.version,
            lambda current: self._mark_background_failed(current, normalized),
        )
        if attempt_id and self.attempts.is_active(session_id, attempt_id):
            self.attempts.finish(session_id, attempt_id, "failed", normalized)
        self.events.append(
            session_id,
            kind="stage_state",
            status="failed",
            stage=str(state.stage),
            attempt_id=attempt_id,
            report=message,
            public_data={"failure_code": code},
        )
        return failed

    @staticmethod
    def _mark_background_failed(current: BuildState, failure: dict[str, Any]) -> BuildState:
        current.status = "failed"
        current.active_attempt_id = ""
        current.failure = dict(failure)
        return current

    @staticmethod
    def _mark_retry(current: BuildState, reason: str) -> BuildState:
        current.status = "running"
        current.active_attempt_id = ""
        current.retry_reason = reason
        current.failure = None
        return current

    def cancel(
        self,
        session_id: str,
        *,
        expected_version: int | None = None,
        reason: str = "",
    ) -> BuildState:
        state = self.states.load(session_id)
        if expected_version is not None and state.version != expected_version:
            raise StateConflictError(
                "Builder version changed before cancellation",
                expected_version=expected_version,
                current_version=state.version,
            )
        if state.status in {"workspace_ready", "failed", "cancelled"}:
            raise StateConflictError("Builder is already terminal", status=state.status)
        interruption = str(reason or "").strip()
        failure_code = "host_interrupted" if interruption else "cancelled_by_user"
        failure_message = interruption or "Builder was cancelled by the user."
        attempt_id = state.active_attempt_id
        cancelled = self.states.update(
            session_id,
            state.version,
            lambda current: self._mark_cancelled(current, failure_code, failure_message),
        )
        if attempt_id:
            self.agent_runner.cancel(attempt_id)
            if self.attempts.is_active(session_id, attempt_id):
                self.attempts.finish(
                    session_id,
                    attempt_id,
                    "cancelled",
                    {"code": failure_code, "message": failure_message},
                )
        self.events.append(
            session_id,
            kind="stage_state",
            status="cancelled",
            stage=str(state.stage),
            attempt_id=attempt_id,
            report=failure_message,
        )
        return cancelled

    @staticmethod
    def _mark_cancelled(
        current: BuildState,
        code: str = "cancelled_by_user",
        message: str = "Builder was cancelled by the user.",
    ) -> BuildState:
        current.status = "cancelled"
        current.active_attempt_id = ""
        current.failure = {"code": code, "message": message}
        return current

    def delete(self, session_id: str) -> dict[str, Any]:
        state = self.states.load(session_id)
        attempt_id = state.active_attempt_id
        if attempt_id:
            state = self.states.update(session_id, state.version, self._mark_cancelled)
            self.agent_runner.cancel(attempt_id)
            if self.attempts.is_active(session_id, attempt_id):
                self.attempts.finish(
                    session_id,
                    attempt_id,
                    "cancelled",
                    {"code": "session_deleted"},
                )
        paths = self.layout.session(session_id)
        with self.locks.acquire(session_id):
            mark_deleted(paths.data_root, session_id)
            shutil.rmtree(paths.root, ignore_errors=False)
        return {"ok": True, "status": "deleted", "session_id": session_id, "workspace_id": session_id}

    def recover_interrupted(self) -> int:
        recovered = 0
        for state in self.states.list_states(limit=10_000):
            if state.status != "running" or not state.active_attempt_id:
                continue
            # Liveness guard: a session whose attempt/events were touched very recently is
            # almost certainly a *live* build owned by another process (e.g. the eval driver)
            # rather than a service-restart orphan. Marking it failed would wedge that live
            # run, so leave it alone. Recovery only reaps sessions with no recent activity.
            if self._session_shows_recent_activity(state.session_id):
                continue
            attempt_id = state.active_attempt_id
            if self.attempts.is_active(state.session_id, attempt_id):
                self.attempts.finish(
                    state.session_id,
                    attempt_id,
                    "failed",
                    {"code": "service_restart", "message": "Agent invocation was interrupted by service restart."},
                )
            try:
                self.states.update(state.session_id, state.version, self._mark_interrupted)
            except (MissingStateError, StateConflictError):
                # Session state vanished (partial/killed session) or changed underneath
                # recovery. Startup must stay crash-safe: skip it rather than abort the
                # whole recovery pass and take the host integration down.
                continue
            self.events.append(
                state.session_id,
                kind="stage_state",
                status="failed",
                stage=str(state.stage),
                report="Agent invocation was interrupted by service restart.",
            )
            recovered += 1
        return recovered

    def _session_shows_recent_activity(self, session_id: str) -> bool:
        """Return True when the session's events or active-attempt file was modified within
        the liveness window — a cheap cross-process signal that a build is still running."""
        now = time.time()
        try:
            paths = self.layout.session(session_id)
        except Exception:  # noqa: BLE001 - an unreadable session is not "live"
            return False
        candidates = [paths.intermediate / "events.jsonl"]
        # state.active_attempt_id is checked by callers, but the attempt file is the
        # strongest liveness signal; include every attempt file to stay conservative.
        attempts_dir = paths.attempts
        if attempts_dir.is_dir():
            candidates.extend(attempts_dir.glob("*.json"))
        for candidate in candidates:
            try:
                if candidate.is_file() and (now - candidate.stat().st_mtime) < RECOVERY_LIVENESS_SECONDS:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _mark_interrupted(current: BuildState) -> BuildState:
        current.status = "failed"
        current.active_attempt_id = ""
        current.failure = {
            "code": "service_restart",
            "message": "Agent invocation was interrupted by service restart.",
        }
        return current
