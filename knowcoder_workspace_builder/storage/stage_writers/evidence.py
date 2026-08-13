"""Data Collector candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_evidence_candidate

from .base import BaseStageWriter


class EvidenceWriter(BaseStageWriter):
    stage = "evidence"
    tool_name = "save_evidence_manifest"
    error_type = "invalid_evidence_manifest"

    def save(self, *, coverage: list[dict[str, Any]], unresolved_gaps: list[str]) -> str:
        def operation() -> dict[str, Any]:
            context = self.context()
            manifest, changes = normalize_evidence_candidate(
                coverage=coverage,
                unresolved_gaps=unresolved_gaps,
                stage_input=context.input,
            )
            target = self.persist("evidence_manifest", manifest)
            return {
                "candidate_path": self.virtual(target),
                "normalization_log": self.normalization_log(changes),
            }

        return self.execute(operation)
