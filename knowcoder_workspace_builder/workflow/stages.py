"""Builder stage identifiers and terminal states."""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    PROBLEM = "problem"
    EVIDENCE = "evidence"
    SCHEMA_BUILD = "schema_build"
    SCHEMA_JUDGE = "schema_judge"
    EXTRACT = "extract"
    STRUCTURED_EXTRACT = "structured_extract"
    DOCUMENT = "document"
    FINALIZE = "finalize"
    READY = "ready"


AGENT_FOR_STAGE: dict[Stage, str] = {
    Stage.PROBLEM: "problem_clarifier",
    Stage.EVIDENCE: "evidence_collector",
    Stage.SCHEMA_BUILD: "schema_builder",
    Stage.SCHEMA_JUDGE: "schema_judger",
    Stage.EXTRACT: "data_extractor",
    Stage.STRUCTURED_EXTRACT: "structured_data_extractor",
    Stage.DOCUMENT: "workspace_documenter",
}


PIPELINE: tuple[Stage, ...] = (
    Stage.PROBLEM,
    Stage.EVIDENCE,
    Stage.SCHEMA_BUILD,
    Stage.SCHEMA_JUDGE,
    Stage.EXTRACT,
    Stage.STRUCTURED_EXTRACT,
    Stage.DOCUMENT,
    Stage.FINALIZE,
    Stage.READY,
)
