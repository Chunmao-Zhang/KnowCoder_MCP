"""Change-impact classification and restart-stage selection."""

from __future__ import annotations

from dataclasses import dataclass

from knowcoder_workspace_builder.contracts.errors import ContractError, StateConflictError
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import PIPELINE, Stage
from knowcoder_workspace_builder.workflow.transitions import (
    ChangeImpact,
    RESTART_STAGE,
    ResearchScopeChange,
)


@dataclass(frozen=True)
class FollowUpPlan:
    request: str
    impact: ChangeImpact
    submitted_impacts: tuple[ChangeImpact, ...]
    research_scope_change: ResearchScopeChange
    evidence_step_indexes: tuple[int, ...]
    restart_stage: Stage | None

    @classmethod
    def create(
        cls,
        request: str,
        impacts: list[str] | tuple[str, ...] | None = None,
        *,
        evidence_step_indexes: list[int] | tuple[int, ...] | None = None,
    ) -> "FollowUpPlan":
        normalized_request = str(request or "").strip()
        if not normalized_request:
            raise ContractError("Completed Workspace follow-up requires the user's request")
        raw_impacts = [str(item or "").strip() for item in (impacts or [])]
        try:
            requested = list(dict.fromkeys(ChangeImpact(item) for item in raw_impacts))
        except ValueError as exc:
            supported = [item.value for item in ChangeImpact]
            raise ContractError(
                "Follow-up change impact is invalid",
                submitted=raw_impacts,
                supported=supported,
            ) from exc
        scope_change = (
            ResearchScopeChange.CHANGED
            if ChangeImpact.PROBLEM in requested
            else ResearchScopeChange.UNCHANGED
        )
        raw_step_indexes = list(evidence_step_indexes or [])
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in raw_step_indexes):
            raise ContractError(
                "evidence_step_indexes must contain positive one-based integers",
                submitted=raw_step_indexes,
            )
        step_indexes = tuple(dict.fromkeys(raw_step_indexes))
        if scope_change == ResearchScopeChange.CHANGED or ChangeImpact.PROBLEM in requested:
            scope_change = ResearchScopeChange.CHANGED
            for implied in (ChangeImpact.PROBLEM, ChangeImpact.EVIDENCE, ChangeImpact.INSTANCES):
                if implied not in requested:
                    requested.append(implied)
            step_indexes = ()
        if step_indexes:
            for implied in (ChangeImpact.EVIDENCE, ChangeImpact.INSTANCES):
                if implied not in requested:
                    requested.append(implied)
        if ChangeImpact.EVIDENCE in requested and ChangeImpact.INSTANCES not in requested:
            requested.append(ChangeImpact.INSTANCES)
        if ChangeImpact.SCHEMA in requested and ChangeImpact.INSTANCES not in requested:
            requested.append(ChangeImpact.INSTANCES)
        if len(requested) > 1 and ChangeImpact.ANSWER_ONLY in requested:
            requested.remove(ChangeImpact.ANSWER_ONLY)
        if not requested:
            raise ContractError(
                "Follow-up requires the earliest affected layer",
                supported=[item.value for item in ChangeImpact],
            )
        submitted = tuple(requested)
        changed = [item for item in submitted if RESTART_STAGE[item] is not None]
        normalized_impact = min(
            changed,
            key=lambda item: PIPELINE.index(RESTART_STAGE[item]),
            default=ChangeImpact.ANSWER_ONLY,
        )
        return cls(
            normalized_request,
            normalized_impact,
            submitted,
            scope_change,
            step_indexes,
            RESTART_STAGE[normalized_impact],
        )

    def validate(self, state: BuildState) -> None:
        if state.status != "workspace_ready" or state.stage != Stage.READY:
            raise StateConflictError(
                "Follow-up impact can be applied only to a completed Workspace",
                status=state.status,
            )
        steps = (state.problem or {}).get("steps")
        if not isinstance(steps, list) or not steps:
            raise ContractError("Completed Workspace is missing confirmed research steps")
        outside = [index for index in self.evidence_step_indexes if index > len(steps)]
        if outside:
            raise ContractError(
                "evidence_step_indexes exceed the confirmed Problem step range",
                submitted=outside,
                step_count=len(steps),
            )

    def resolved_evidence_step_indexes(self, state: BuildState) -> tuple[int, ...]:
        if self.evidence_step_indexes:
            return self.evidence_step_indexes
        if (
            self.research_scope_change != ResearchScopeChange.UNCHANGED
            or ChangeImpact.EVIDENCE not in self.submitted_impacts
        ):
            return ()
        steps = (state.problem or {}).get("steps")
        if not isinstance(steps, list) or not steps:
            raise ContractError("Completed Workspace is missing confirmed research steps")
        return tuple(range(1, len(steps) + 1))

    def apply(self, state: BuildState) -> BuildState:
        if self.restart_stage is None:
            return state

        restart_index = PIPELINE.index(self.restart_stage)
        state.accepted_attempts = {
            stage: attempt_id
            for stage, attempt_id in state.accepted_attempts.items()
            if PIPELINE.index(Stage(stage)) < restart_index
        }
        state.stage = self.restart_stage
        state.status = "running"
        state.active_attempt_id = ""
        state.invalidated_from = self.restart_stage
        state.pending_revision = self.request
        state.active_follow_up_request = self.request
        state.retry_reason = ""
        state.pending_evidence_step_indexes = list(self.resolved_evidence_step_indexes(state))
        state.failure = None
        # Direct instance revisions and Schema revisions rebuild the complete
        # instance layer from the accepted sources. Evidence and problem
        # extensions retain the immutable baseline and append affected facts.
        state.replace_instances = self.restart_stage in {
            Stage.SCHEMA_BUILD,
            Stage.EXTRACT,
        }
        if state.replace_instances or self.restart_stage == Stage.PROBLEM:
            state.extraction = {}
        if restart_index <= PIPELINE.index(Stage.SCHEMA_BUILD):
            state.schema_confirmed = False
        if self.restart_stage == Stage.PROBLEM:
            steps = (state.problem or {}).get("steps")
            if not isinstance(steps, list) or not steps:
                raise ContractError("Completed Workspace is missing the in-place extension baseline steps")
            state.extension_baseline_steps = [str(step) for step in steps if str(step).strip()]
            state.problem_confirmed = False
        return state
