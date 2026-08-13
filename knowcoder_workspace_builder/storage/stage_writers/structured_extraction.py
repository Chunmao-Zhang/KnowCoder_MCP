"""Structured Data Extractor candidate writer."""

from __future__ import annotations

import json
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_instance_batch
from knowcoder_workspace_builder.storage.stage_artifacts import artifact_path

from .instances_base import InstanceStageWriter


class StructuredExtractionWriter(InstanceStageWriter):
    stage = "structured_extract"
    tool_name = "append_instances_batches_from_file"
    error_type = "invalid_structured_batch"

    def save(self) -> str:
        try:
            context = self.context()
            batch_path = artifact_path(self.paths(), context.attempt_id, "structured_batches")
            value = json.loads(batch_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("batches"), list):
                raise ContractError("Structured batch file requires a batches list")
            changes = [
                {
                    "field": field,
                    "action": "ignored",
                    "detail": "Ignored a field outside the canonical structured batch contract.",
                }
                for field in sorted(set(value) - {"batches"})
            ]
            entities: list[dict[str, Any]] = []
            relations: list[dict[str, Any]] = []
            for position, batch in enumerate(value["batches"], start=1):
                if not isinstance(batch, dict):
                    raise ContractError("Every structured batch must be an object", position=position)
                if not isinstance(batch.get("entities"), list) or not isinstance(batch.get("relations"), list):
                    raise ContractError("Every structured batch requires entities and relations lists")
                changes.extend(
                    {
                        "field": f"batches[{position}].{field}",
                        "action": "ignored",
                        "detail": "Ignored a field outside the canonical structured batch item contract.",
                    }
                    for field in sorted(set(batch) - {"entities", "relations"})
                )
                entities.extend(batch["entities"])
                relations.extend(batch["relations"])
            source_ids = self.expected_source_ids()
            normalized_entities, normalized_relations, instance_changes = normalize_instance_batch(
                entities=entities,
                relations=relations,
                source_ids=source_ids,
            )
            changes.extend(instance_changes)
            draft, target = self.append_draft(
                "structured_draft",
                normalized_entities,
                normalized_relations,
                source_ids,
            )
            changes.extend(
                [
                    {"field": "batch_path", "action": "derived", "detail": "Resolved the active attempt batch file."},
                    {"field": "processed_source_ids", "action": "derived", "detail": "Copied every assigned source ID."},
                    {"field": "extraction_complete", "action": "derived", "detail": "Computed after complete source coverage."},
                ]
            )
            return self.response(
                {
                    "ok": True,
                    "draft_path": self.virtual(target),
                    "processed_source_ids": draft["processed_source_ids"],
                    "entity_count": len(draft["entities"]),
                    "relation_count": len(draft["relations"]),
                    "extraction_complete": True,
                    "normalization_log": self.normalization_log(changes),
                }
            )
        except self.expected_errors as exc:
            payload = self.invalid_batch_payload(exc)
            payload["error_type"] = self.error_type
            return self.response(payload)
