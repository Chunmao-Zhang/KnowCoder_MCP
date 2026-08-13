"""Allowed workflow transitions and invalidation rules."""

from __future__ import annotations

from enum import StrEnum

from knowcoder_workspace_builder.contracts.errors import ContractError

from .stages import PIPELINE, Stage


class ChangeImpact(StrEnum):
    ANSWER_ONLY = "answer_only"
    INSTANCES = "instances"
    SCHEMA = "schema"
    EVIDENCE = "evidence"
    PROBLEM = "problem"


class ResearchScopeChange(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"


RESTART_STAGE: dict[ChangeImpact, Stage | None] = {
    ChangeImpact.ANSWER_ONLY: None,
    ChangeImpact.INSTANCES: Stage.EXTRACT,
    ChangeImpact.SCHEMA: Stage.SCHEMA_BUILD,
    ChangeImpact.EVIDENCE: Stage.EVIDENCE,
    ChangeImpact.PROBLEM: Stage.PROBLEM,
}


def follows(current: Stage, candidate: Stage) -> bool:
    return PIPELINE.index(candidate) == PIPELINE.index(current) + 1


def require_transition(current: Stage, candidate: Stage, *, allow_restart: bool = False) -> None:
    if current == candidate:
        return
    if follows(current, candidate):
        return
    if allow_restart and PIPELINE.index(candidate) < PIPELINE.index(current):
        return
    raise ContractError("Invalid Builder stage transition", current=current, candidate=candidate)
