"""Problem Analyst candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_problem_candidate

from .base import BaseStageWriter


class ProblemWriter(BaseStageWriter):
    stage = "problem"
    tool_name = "save_problem_review"
    error_type = "problem_candidate_write_failed"

    def save(
        self,
        *,
        workspace_action: str,
        scope: dict[str, Any],
        steps: list[str],
        missing_information: list[str],
        base_workspace_id: str | None,
    ) -> str:
        def operation() -> dict[str, Any]:
            context = self.context()
            payload, changes = normalize_problem_candidate(
                workspace_action=workspace_action,
                base_workspace_id=base_workspace_id,
                scope=scope,
                steps=steps,
                missing_information=missing_information,
                stage_input=context.input,
            )
            target = self.persist("problem_review", payload)
            return {
                "candidate_path": self.virtual(target),
                "normalization_log": self.normalization_log(changes),
            }

        return self.execute(operation)
