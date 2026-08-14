"""Data Collector candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.runtime.candidate_normalization import (
    normalize_evidence_candidate,
)
from knowcoder_workspace_builder.tools.web_fetch import (
    prepare_fetch_candidates,
    register_prepared_fetch_sources,
)

from .base import BaseStageWriter


class EvidenceWriter(BaseStageWriter):
    stage = "evidence"
    tool_name = "save_evidence_manifest"
    error_type = "invalid_evidence_manifest"

    def save(
        self,
        *,
        coverage: list[dict[str, Any]],
        selected_web_sources: list[dict[str, Any]],
        unresolved_gaps: list[str],
    ) -> str:
        def operation() -> dict[str, Any]:
            context = self.context()
            selected_bindings, selected_records = prepare_fetch_candidates(selected_web_sources)
            formal_records = [
                {key: value for key, value in record.items() if not key.startswith("_candidate_")}
                for record in selected_records
            ]
            manifest, changes = normalize_evidence_candidate(
                coverage=coverage,
                unresolved_gaps=unresolved_gaps,
                stage_input=context.input,
                selected_web_bindings=selected_bindings,
                selected_web_records=formal_records,
            )
            normalization_log = self.normalization_log(changes)
            register_prepared_fetch_sources(selected_records)
            target = self.persist("evidence_manifest", manifest)
            return {
                "candidate_path": self.virtual(target),
                "normalization_log": normalization_log,
            }

        return self.execute(operation)
