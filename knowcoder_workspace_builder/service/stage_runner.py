"""Invoke one declared stage and atomically accept or reject its result."""

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from knowcoder_workspace_builder.agents.runner import AgentRunner
from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import (
    AttemptCancelledError,
    BuilderError,
    ContractError,
    StateConflictError,
)
from knowcoder_workspace_builder.runtime.live_events import publish_builder_event
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_session_path
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.canonical import publish_stage_result
from knowcoder_workspace_builder.storage.events import EventStore
from knowcoder_workspace_builder.storage.extensions import install_extension_baseline
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.stage_artifacts import empty_draft, write_artifact
from knowcoder_workspace_builder.validation.inputs import validate_stage_input
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import AGENT_FOR_STAGE, Stage


class StageRunner:
    def __init__(self, layout: ProjectLayout, agent_runner: AgentRunner) -> None:
        self.layout = layout
        self.agent_runner = agent_runner
        self.states = BuildStateStore(layout)
        self.attempts = AttemptStore(layout)
        self.events = EventStore(layout)

    def run(
        self,
        state: BuildState,
        stage_input: dict[str, object],
        *,
        turn_id: str = "",
    ) -> BuildState:
        stage = Stage(state.stage)
        if stage not in AGENT_FOR_STAGE:
            raise StateConflictError("Current stage has no Subagent", stage=stage)
        if state.active_attempt_id:
            raise StateConflictError(
                "Session already has an active Agent attempt",
                session_id=state.session_id,
                attempt_id=state.active_attempt_id,
            )
        number = int(state.stage_attempts.get(stage, 0)) + 1
        attempt_id = str(uuid4())
        normalized_input = dict(stage_input)
        if stage in {Stage.EXTRACT, Stage.STRUCTURED_EXTRACT}:
            artifact = "unstructured_draft.json" if stage == Stage.EXTRACT else "structured_draft.json"
            normalized_input["draft_path"] = virtual_session_path(f"intermediate/attempts/{attempt_id}/{artifact}")
            if stage == Stage.STRUCTURED_EXTRACT:
                normalized_input["batch_path"] = virtual_session_path(
                    f"intermediate/attempts/{attempt_id}/structured_batches.json"
                )
        normalized_input = validate_stage_input(stage, normalized_input)
        attempt = self.attempts.start(state.session_id, stage, number, attempt_id=attempt_id)
        attempt_id = str(attempt["attempt_id"])
        coordinator_invocation = str(uuid4())
        subagent_invocation = str(uuid4())
        owner_turn = turn_id.strip() or attempt_id

        try:
            running = self.states.update(
                state.session_id,
                state.version,
                lambda current: self._mark_running(current, stage, attempt_id, number),
            )
        except BuilderError as exc:
            if self.attempts.is_active(state.session_id, attempt_id):
                self.attempts.finish(state.session_id, attempt_id, "failed", exc.detail.to_dict())
            raise
        self._start_events(
            running,
            stage,
            attempt_id,
            number,
            owner_turn,
            coordinator_invocation,
        )
        model_snapshot: dict[str, str] = {}
        subagent_started = False
        # Parallel extract units share this callback from multiple threads; serialize
        # the start-flag transition and event append so concurrent units cannot double
        # start the Subagent lane or interleave writes to the model snapshot.
        event_lock = threading.Lock()

        def start_subagent() -> None:
            nonlocal subagent_started
            if subagent_started:
                return
            self._start_subagent_events(
                running,
                stage,
                attempt_id,
                number,
                owner_turn,
                subagent_invocation,
            )
            self._publish_stage_event(
                running,
                stage,
                attempt_id,
                number,
                owner_turn,
                subagent_invocation,
                "running",
            )
            subagent_started = True

        def _on_agent_event_locked(event: dict[str, Any]) -> None:
            # The configured stage owner (harness required_subagent) is the source of
            # truth for which Subagent may run. The dispatch notification is emitted
            # asynchronously and can arrive before, after, or independently of the
            # Subagent's first model/tool event, so ordering alone must not fail the
            # stage. Any event from the declared owner starts the Subagent lane; only a
            # genuinely different Agent is a conflict.
            event_agent = str(event.get("agent") or "").strip()
            run_agent = str(event.get("run_agent") or "").strip()
            if event_agent == "workspace_builder" or (not event_agent and run_agent == "workspace_builder"):
                invocation_id = coordinator_invocation
            elif event_agent == AGENT_FOR_STAGE[stage]:
                start_subagent()
                invocation_id = subagent_invocation
            elif not event_agent:
                # One StageRunner runs a single stage, so an execution event without an
                # agent name belongs to the current stage Subagent (the Coordinator tags
                # its own activity with run_agent=workspace_builder). Treat it as owner
                # activity so anonymous Subagent streams cannot be misread as a conflict.
                start_subagent()
                invocation_id = subagent_invocation
            else:
                raise StateConflictError(
                    "Builder emitted an event for an unexpected Agent",
                    stage=str(stage),
                    expected=AGENT_FOR_STAGE[stage],
                    actual=event_agent,
                )
            payload = self._live_event(
                event,
                running,
                stage,
                attempt_id,
                number,
                owner_turn,
                invocation_id,
            )
            if payload.get("type") == "stream" and invocation_id == subagent_invocation:
                model_snapshot.update(
                    stream_id=str(payload.get("stream_id") or ""),
                    thinking=str(payload.get("thinking") or ""),
                    output=str(payload.get("output") or ""),
                )
            publish_builder_event(payload)

        def on_agent_event(event: dict[str, Any]) -> None:
            with event_lock:
                _on_agent_event_locked(event)

        try:
            if stage == Stage.EXTRACT and not (normalized_input.get("sources") or []):
                result = self._skip_unstructured_extract_without_sources(
                    paths=self.layout.session(state.session_id),
                    attempt_id=attempt_id,
                )
            elif stage == Stage.STRUCTURED_EXTRACT and not (normalized_input.get("sources") or []):
                # No structured sources were classified for this question. Persist an empty
                # structured draft deterministically and skip the stage instead of asking the
                # model to write a batch file for nothing — that model-written file path was
                # the fragile link that kept failing (virtual path written as an OS path).
                result = self._skip_structured_extract_without_sources(
                    paths=self.layout.session(state.session_id),
                    attempt_id=attempt_id,
                )
            else:
                result = self.agent_runner.run(
                    stage=stage,
                    stage_input=normalized_input,
                    paths=self.layout.session(state.session_id),
                    attempt_id=attempt_id,
                    on_event=on_agent_event,
                )
            # The result itself is authoritative. A started Subagent lane only drives the
            # live display, so a stage that produced a result without any Subagent event
            # (a lightweight or event-less runner) is still valid. A genuinely wrong Agent
            # is already rejected inside on_agent_event, and attempt ownership is checked
            # below, so a missing Subagent lane is not, by itself, an error.
            if not self.attempts.is_active(state.session_id, attempt_id):
                raise AttemptCancelledError(
                    "Agent result arrived after its attempt became inactive",
                    session_id=state.session_id,
                    attempt_id=attempt_id,
                )
            baseline_state: BuildState | None = None
            if stage == Stage.PROBLEM and result.handoff.get("workspace_action") == "extend":
                base_workspace_id = str(result.handoff.get("base_workspace_id") or "").strip()
                workspace_context = normalized_input.get("workspace_context")
                required_base_workspace_id = (
                    str(workspace_context.get("required_base_workspace_id") or "").strip()
                    if isinstance(workspace_context, dict)
                    else ""
                )
                if required_base_workspace_id and base_workspace_id != required_base_workspace_id:
                    raise ContractError(
                        "In-place extension selected an unexpected baseline Workspace",
                        expected=required_base_workspace_id,
                        actual=base_workspace_id,
                    )
                baseline = install_extension_baseline(self.layout, state.session_id, base_workspace_id)
                if base_workspace_id != state.session_id:
                    baseline_state = BuildState.from_dict(baseline)
            if result.ok:
                publish_stage_result(
                    self.layout.session(state.session_id),
                    str(stage),
                    attempt_id,
                    result,
                )
            accepted = self.states.update(
                state.session_id,
                running.version,
                lambda current: self._accept(
                    current,
                    attempt_id,
                    result,
                    baseline_state=baseline_state,
                    evidence_step_indexes=(
                        list(normalized_input.get("workspace_context", {}).get("uncovered_step_indexes") or [])
                        if stage == Stage.EVIDENCE and isinstance(normalized_input.get("workspace_context"), dict)
                        else None
                    ),
                ),
            )
            attempt_status = "completed" if result.ok else "failed"
            attempt_error = None if result.ok else {"errors": list(result.errors), "report": result.report}
            self.attempts.finish(state.session_id, attempt_id, attempt_status, attempt_error)
            # A stage that finished without any Subagent event (e.g. an empty extraction
            # skip) never opened its Subagent invocation. Open it now so the invocation
            # still starts with a running event before the completion events are appended;
            # the projection requires every invocation to begin with running.
            if not subagent_started:
                start_subagent()
            self._complete_events(
                accepted,
                result,
                stage,
                attempt_id,
                number,
                owner_turn,
                coordinator_invocation,
                subagent_invocation,
                model_snapshot,
            )
            self._publish_stage_event(
                accepted,
                stage,
                attempt_id,
                number,
                owner_turn,
                subagent_invocation,
                "done" if result.ok else "failed",
            )
            return accepted
        except BuilderError as exc:
            try:
                return self._fail_attempt(
                    running,
                    stage,
                    attempt_id,
                    number,
                    owner_turn,
                    coordinator_invocation,
                    subagent_invocation,
                    exc,
                    model_snapshot,
                    subagent_started,
                )
            except BuilderError:
                # A deleted Session has no state or event files left to update.
                raise exc

    @staticmethod
    def _mark_running(current: BuildState, stage: Stage, attempt_id: str, number: int) -> BuildState:
        if current.active_attempt_id:
            raise StateConflictError("Session acquired another active attempt", attempt_id=current.active_attempt_id)
        if current.stage != stage:
            raise StateConflictError("Builder stage changed before attempt start", expected=stage, actual=current.stage)
        current.active_attempt_id = attempt_id
        current.stage_attempts[str(stage)] = number
        current.status = "running"
        current.failure = None
        return current

    @staticmethod
    def _accept(
        current: BuildState,
        attempt_id: str,
        result: StageResult,
        *,
        baseline_state: BuildState | None = None,
        evidence_step_indexes: list[int] | None = None,
    ) -> BuildState:
        if current.active_attempt_id != attempt_id:
            raise StateConflictError("Late Agent result is not owned by the active attempt", attempt_id=attempt_id)
        if current.stage != result.stage:
            raise StateConflictError(
                "Agent result stage changed before acceptance", expected=current.stage, actual=result.stage
            )
        current.active_attempt_id = ""
        current.accepted_attempts[result.stage] = attempt_id
        if not result.ok:
            current.status = "failed"
            current.failure = {
                "code": "stage_failed",
                "stage": result.stage,
                "errors": list(result.errors),
                "report": result.report,
            }
            return current

        handoff = dict(result.handoff)
        artifacts = dict(result.artifacts)
        current.retry_reason = ""
        if result.stage == Stage.PROBLEM:
            if baseline_state is not None:
                current.evidence = baseline_state.evidence
                current.schema_review = baseline_state.schema_review
                current.schema_confirmed = baseline_state.schema_confirmed
                current.extraction = dict(baseline_state.extraction)
                current.documentation = baseline_state.documentation
                baseline_steps = (baseline_state.problem or {}).get("steps")
                if isinstance(baseline_steps, list):
                    current.extension_baseline_steps = [
                        str(step).strip() for step in baseline_steps if str(step).strip()
                    ]
            current.workspace_mode = str(handoff.get("workspace_action") or "new")
            current.base_workspace_id = str(handoff.get("base_workspace_id") or "")
            current.problem = {**handoff, "artifacts": artifacts}
            current.problem_confirmed = False
            current.pending_revision = ""
            current.status = "needs_problem_confirmation"
        elif result.stage == Stage.EVIDENCE:
            current.evidence = {**handoff, "artifacts": artifacts}
            current.pending_evidence_step_indexes = [
                index
                for index in (evidence_step_indexes or [])
                if isinstance(index, int) and not isinstance(index, bool) and index > 0
            ]
            current.stage = Stage.SCHEMA_BUILD
        elif result.stage == Stage.SCHEMA_BUILD:
            current.schema_review = {**handoff, "artifacts": artifacts}
            current.stage = Stage.SCHEMA_JUDGE
        elif result.stage == Stage.SCHEMA_JUDGE:
            candidate = dict(current.schema_review or {})
            candidate["judgement"] = {**handoff, "artifacts": artifacts}
            current.schema_review = candidate
            if handoff["decision"] == "revise":
                current.schema_revision_rounds += 1
                current.stage = Stage.SCHEMA_BUILD
            else:
                current.schema_revision_rounds = 0
                current.schema_review["requires_revalidation"] = False
                current.schema_confirmed = False
                current.status = "needs_schema_confirmation"
        elif result.stage == Stage.EXTRACT:
            current.extraction["unstructured"] = StageRunner._merge_extraction_bucket(
                current.extraction.get("unstructured"),
                handoff=handoff,
                artifacts=artifacts,
                status=result.status,
            )
            current.stage = Stage.STRUCTURED_EXTRACT
        elif result.stage == Stage.STRUCTURED_EXTRACT:
            current.extraction["structured"] = StageRunner._merge_extraction_bucket(
                current.extraction.get("structured"),
                handoff=handoff,
                artifacts=artifacts,
                status=result.status,
            )
            current.stage = Stage.DOCUMENT
        elif result.stage == Stage.DOCUMENT:
            current.documentation = {**handoff, "artifacts": artifacts}
            current.stage = Stage.FINALIZE
        return current

    @staticmethod
    def _merge_extraction_bucket(
        previous: object,
        *,
        handoff: dict[str, object],
        artifacts: dict[str, object],
        status: str,
    ) -> dict[str, object]:
        prior = previous if isinstance(previous, dict) else {}
        prior_ids = [str(item).strip() for item in (prior.get("processed_source_ids") or []) if str(item or "").strip()]
        new_ids = [str(item).strip() for item in (handoff.get("processed_source_ids") or []) if str(item or "").strip()]
        merged_ids: list[str] = []
        seen: set[str] = set()
        for source_id in [*prior_ids, *new_ids]:
            if source_id not in seen:
                seen.add(source_id)
                merged_ids.append(source_id)
        entity_count = int(handoff.get("entity_count") or 0)
        relation_count = int(handoff.get("relation_count") or 0)
        if status == "skipped" and prior and str(prior.get("status") or "") not in {"", "skipped"}:
            # Keep previous successful extraction when the new pass had nothing left.
            return {
                **prior,
                "status": str(prior.get("status") or "completed"),
                "processed_source_ids": merged_ids or prior_ids,
            }
        if prior and str(prior.get("status") or "") not in {"", "skipped"}:
            entity_count = max(entity_count, int(prior.get("entity_count") or 0))
            relation_count = max(relation_count, int(prior.get("relation_count") or 0))
        merged_artifacts = {}
        if isinstance(prior.get("artifacts"), dict):
            merged_artifacts.update(prior["artifacts"])
        merged_artifacts.update(artifacts)
        result = {
            **handoff,
            "processed_source_ids": merged_ids,
            "entity_count": entity_count,
            "relation_count": relation_count,
            "artifacts": merged_artifacts,
            "status": status,
        }
        if handoff.get("skip_reason") and status == "skipped":
            result["skip_reason"] = handoff["skip_reason"]
        return result

    def _start_events(
        self,
        state: BuildState,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        coordinator_invocation: str,
    ) -> None:
        common = {
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "stage": str(stage),
            "attempt_number": number,
        }
        self.events.append(
            state.session_id,
            kind="invocation",
            status="running",
            invocation_id=coordinator_invocation,
            agent="workspace_builder",
            **common,
        )

    def _start_subagent_events(
        self,
        state: BuildState,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        subagent_invocation: str,
    ) -> None:
        common = {
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "stage": str(stage),
            "attempt_number": number,
        }
        self.events.append(
            state.session_id,
            kind="invocation",
            status="running",
            invocation_id=subagent_invocation,
            agent=AGENT_FOR_STAGE[stage],
            **common,
        )
        self.events.append(
            state.session_id,
            kind="stage_state",
            status="running",
            agent=AGENT_FOR_STAGE[stage],
            **common,
        )

    def _complete_events(
        self,
        state: BuildState,
        result: StageResult,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        coordinator_invocation: str,
        subagent_invocation: str,
        model_snapshot: dict[str, str],
    ) -> None:
        coordinator_report = (
            f"Delegated {stage} to {AGENT_FOR_STAGE[stage]} and accepted its validated result."
            if result.ok
            else f"Delegated {stage} to {AGENT_FOR_STAGE[stage]} and rejected its invalid result."
        )
        common = {
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "stage": str(stage),
            "attempt_number": number,
        }
        terminal_status = result.status
        public_data: dict[str, Any] = {"artifacts": dict(result.artifacts)}
        if model_snapshot.get("thinking") or model_snapshot.get("output"):
            public_data["model"] = dict(model_snapshot)
        self.events.append(
            state.session_id,
            kind="invocation",
            status=terminal_status,
            invocation_id=subagent_invocation,
            agent=AGENT_FOR_STAGE[stage],
            report=result.report,
            public_data=public_data,
            **common,
        )
        self.events.append(
            state.session_id,
            kind="invocation",
            status="completed" if result.ok else "failed",
            invocation_id=coordinator_invocation,
            agent="workspace_builder",
            report=coordinator_report,
            public_data={},
            **common,
        )
        self.events.append(
            state.session_id,
            kind="handoff",
            status=terminal_status,
            visibility="private",
            agent=AGENT_FOR_STAGE[stage],
            private_data=dict(result.handoff),
            **common,
        )
        stage_status = "waiting" if state.status.startswith("needs_") else terminal_status
        self.events.append(
            state.session_id,
            kind="stage_state",
            status=stage_status,
            agent=AGENT_FOR_STAGE[stage],
            **common,
        )

    def _fail_attempt(
        self,
        running: BuildState,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        coordinator_invocation: str,
        subagent_invocation: str,
        error: BuilderError,
        model_snapshot: dict[str, str],
        subagent_started: bool,
    ) -> BuildState:
        current_snapshot = self.states.load(running.session_id)
        cancelled = isinstance(error, AttemptCancelledError) or current_snapshot.status == "cancelled"
        terminal = "cancelled" if cancelled else "failed"
        if self.attempts.is_active(running.session_id, attempt_id):
            self.attempts.finish(running.session_id, attempt_id, terminal, error.detail.to_dict())
        try:
            failed = self.states.update(
                running.session_id,
                running.version,
                lambda current: self._mark_failed(current, attempt_id, error, cancelled),
            )
        except StateConflictError:
            failed = self.states.load(running.session_id)
        report = str(error)
        common = {
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "stage": str(stage),
            "attempt_number": number,
        }
        invocations = [(coordinator_invocation, "workspace_builder")]
        if subagent_started:
            invocations.insert(0, (subagent_invocation, AGENT_FOR_STAGE[stage]))
        for invocation_id, agent in invocations:
            public_data: dict[str, Any] = {"error": error.detail.to_dict()}
            if invocation_id == subagent_invocation and (
                model_snapshot.get("thinking") or model_snapshot.get("output")
            ):
                public_data["model"] = dict(model_snapshot)
            self.events.append(
                running.session_id,
                kind="invocation",
                status=terminal,
                invocation_id=invocation_id,
                agent=agent,
                report=report,
                public_data=public_data,
                **common,
            )
        self.events.append(
            running.session_id,
            kind="stage_state",
            status=terminal,
            agent=AGENT_FOR_STAGE[stage],
            public_data={"error": error.detail.to_dict()},
            **common,
        )
        if subagent_started:
            self._publish_stage_event(
                failed,
                stage,
                attempt_id,
                number,
                turn_id,
                subagent_invocation,
                terminal,
            )
        return failed

    @staticmethod
    def _live_event(
        event: dict[str, Any],
        state: BuildState,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        invocation_id: str,
    ) -> dict[str, Any]:
        payload = {
            **event,
            "session_id": state.session_id,
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "invocation_id": invocation_id,
            "stage": str(stage),
            "run_index": int(event.get("run_index") or number),
            "stage_attempt_number": number,
            "run_agent": "workspace_builder",
        }
        message = payload.get("message")
        if isinstance(message, dict):
            message_run_index = int(
                message.get("extract_unit_index")
                or message.get("run_index")
                or event.get("extract_unit_index")
                or event.get("run_index")
                or number
            )
            payload["message"] = {
                **message,
                "turn_id": turn_id,
                "attempt_id": attempt_id,
                "invocation_id": invocation_id,
                "stage": str(stage),
                "run_index": message_run_index,
                "run_agent": "workspace_builder",
            }
        return payload

    @staticmethod
    def _publish_stage_event(
        state: BuildState,
        stage: Stage,
        attempt_id: str,
        number: int,
        turn_id: str,
        invocation_id: str,
        status: str,
    ) -> None:
        publish_builder_event(
            {
                "type": "stage",
                "session_id": state.session_id,
                "turn_id": turn_id,
                "attempt_id": attempt_id,
                "invocation_id": invocation_id,
                "stage": str(stage),
                "status": status,
                "run_index": number,
                "run_agent": "workspace_builder",
                "agent": AGENT_FOR_STAGE[stage],
            }
        )

    @staticmethod
    def _mark_failed(current: BuildState, attempt_id: str, error: BuilderError, cancelled: bool) -> BuildState:
        if current.active_attempt_id != attempt_id:
            raise StateConflictError("Failed attempt is no longer active", attempt_id=attempt_id)
        current.active_attempt_id = ""
        current.status = "cancelled" if cancelled else "failed"
        current.failure = error.detail.to_dict()
        return current

    def _skip_structured_extract_without_sources(
        self,
        *,
        paths,
        attempt_id: str,
    ) -> StageResult:
        """Persist an empty structured draft deterministically and skip the stage.

        The question produced no structured sources, so there is nothing to extract.
        Writing the empty draft here (instead of asking the model to) removes the
        model-written-file-path step that kept failing.
        """
        write_artifact(paths, attempt_id, "structured_draft", empty_draft())
        return StageResult(
            ok=True,
            stage=str(Stage.STRUCTURED_EXTRACT),
            status="skipped",
            report="No structured sources were assigned, so structured extraction was skipped.",
            handoff={
                "processed_source_ids": [],
                "entity_count": 0,
                "relation_count": 0,
                "skip_reason": "No structured sources were classified for this question.",
            },
            artifacts={
                "structured_draft": virtual_session_path(f"intermediate/attempts/{attempt_id}/structured_draft.json")
            },
            errors=(),
        )

    def _skip_unstructured_extract_without_sources(
        self,
        *,
        paths,
        attempt_id: str,
    ) -> StageResult:
        write_artifact(paths, attempt_id, "unstructured_draft", empty_draft())
        return StageResult(
            ok=True,
            stage=str(Stage.EXTRACT),
            status="skipped",
            report="No unstructured sources were assigned, so extraction was skipped.",
            handoff={
                "processed_source_ids": [],
                "entity_count": 0,
                "relation_count": 0,
                "skip_reason": "No unstructured sources were classified for this question.",
            },
            artifacts={
                "unstructured_draft": virtual_session_path(
                    f"intermediate/attempts/{attempt_id}/unstructured_draft.json"
                )
            },
            errors=(),
        )
