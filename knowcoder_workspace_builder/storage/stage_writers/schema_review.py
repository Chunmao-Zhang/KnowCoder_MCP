"""Schema Reviewer candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_schema_judgement_candidate

from .base import BaseStageWriter


class SchemaReviewWriter(BaseStageWriter):
    stage = "schema_judge"
    tool_name = "save_schema_judgement"
    error_type = "schema_judgement_write_failed"

    def save(self, *, decision: str, missing_requirements: list[str]) -> str:
        def operation() -> dict[str, Any]:
            judgement, changes = normalize_schema_judgement_candidate(
                decision=decision,
                missing_requirements=missing_requirements,
            )
            target = self.persist(
                "schema_judgement",
                judgement,
            )
            return {
                "candidate_path": self.virtual(target),
                "normalization_log": self.normalization_log(changes),
            }

        return self.execute(operation)
