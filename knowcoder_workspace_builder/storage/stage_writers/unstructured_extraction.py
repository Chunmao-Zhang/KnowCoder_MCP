"""Unstructured Data Extractor candidate writer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_instance_batch
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json
from knowcoder_workspace_builder.validation.incremental import MAX_UNIT_ATTEMPTS
from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt

from .instances_base import InstanceStageWriter


class UnstructuredExtractionWriter(InstanceStageWriter):
    stage = "extract"
    tool_name = "append_instances_batch"
    error_type = "invalid_instance_batch"

    def _attempt_path(self) -> Path:
        context = self.context()
        return self.paths().attempts / context.attempt_id / "unit_attempts.json"

    def _load_attempts(self) -> dict[str, Any]:
        path = self._attempt_path()
        if not path.is_file():
            return {"format_version": 1, "units": {}}
        value = read_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("units"), dict):
            raise ContractError("Extraction unit attempt state is invalid")
        return value

    def _save_attempts(self, value: dict[str, Any]) -> None:
        AtomicWriter(self.paths()).json(self._attempt_path(), value)

    def _unit_key(self, source_ids: list[str]) -> str:
        workspace_context = self.context().input.get("workspace_context")
        step_index = workspace_context.get("current_step_index") if isinstance(workspace_context, dict) else None
        if isinstance(step_index, int) and not isinstance(step_index, bool) and step_index > 0:
            return f"step-{step_index}"
        return ",".join(source_ids) if source_ids else "empty-unit"

    def save(self, *, entities: list[dict[str, Any]], relations: list[dict[str, Any]]) -> str:
        source_ids = self.expected_source_ids()
        unit_key = self._unit_key(source_ids)
        try:
            selected_path = self.paths().attempts / self.context().attempt_id / "selected_chunks.json"
            selected = read_json(selected_path) if selected_path.is_file() else {"evidence_refs": []}
            evidence_refs = selected.get("evidence_refs") if isinstance(selected, dict) else []
            normalized_entities, normalized_relations, changes = normalize_instance_batch(
                entities=entities,
                relations=relations,
                source_ids=source_ids,
                evidence_refs=evidence_refs if isinstance(evidence_refs, list) else [],
            )
            draft, target = self.append_draft(
                "unstructured_draft",
                normalized_entities,
                normalized_relations,
                source_ids,
            )
            changes.extend(
                [
                    {"field": "processed_source_ids", "action": "derived", "detail": "Copied source IDs assigned to the active extraction unit."},
                    {"field": "extraction_complete", "action": "derived", "detail": "Computed after complete source coverage."},
                    {"field": "step_index", "action": "derived", "detail": "Copied from the active workspace context when present."},
                ]
            )
            state = self._load_attempts()
            units = dict(state["units"])
            previous = dict(units.get(unit_key) or {})
            units[unit_key] = {
                "failures": 0,
                "validation_failures": int(previous.get("validation_failures") or previous.get("failures") or 0),
                "last_error": previous.get("last_error"),
            }
            state["units"] = units
            self._save_attempts(state)
            return self.response(
                {
                    "ok": True,
                    "draft_path": self.virtual(target),
                    "processed_source_ids": draft["processed_source_ids"],
                    "entity_count": len(draft["entities"]),
                    "relation_count": len(draft["relations"]),
                    "extraction_complete": True,
                    "unit_id": unit_key,
                    "normalization_log": self.normalization_log(changes),
                }
            )
        except self.expected_errors as exc:
            state = self._load_attempts()
            units = dict(state["units"])
            record = dict(units.get(unit_key) or {"failures": 0})
            record["failures"] = int(record.get("failures") or 0) + 1
            payload = self.invalid_batch_payload(exc)
            record["last_error"] = payload["error"]
            record["last_error_context"] = payload["context"]
            units[unit_key] = record
            state["units"] = units
            self._save_attempts(state)
            payload.update(
                {
                    "unit_id": unit_key,
                    "unit_attempt": record["failures"],
                    "unit_max_attempts": MAX_UNIT_ATTEMPTS,
                    "repair_prompt": resolve_repair_prompt(
                        "extract",
                        mode="incremental",
                        errors=[str(payload["error"])],
                        context=dict(payload["context"]),
                    ),
                }
            )
            if record["failures"] >= MAX_UNIT_ATTEMPTS:
                payload.update(
                    {
                        "error_type": "instance_batch_retry_limit",
                        "validation_error": payload["error"],
                        "error": f"Instance batch failed validation {MAX_UNIT_ATTEMPTS} times.",
                    }
                )
            return self.response(payload)
