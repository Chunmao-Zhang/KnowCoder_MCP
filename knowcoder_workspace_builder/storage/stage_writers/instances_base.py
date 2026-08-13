"""Shared deterministic merge logic for extraction writers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import BuilderError, ContractError
from knowcoder_workspace_builder.storage.stage_artifacts import artifact_path, empty_draft, merge_draft
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json
from knowcoder_workspace_builder.validation.extraction import validate_extraction_draft
from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt

from .base import BaseStageWriter


class InstanceStageWriter(BaseStageWriter):
    def expected_source_ids(self) -> list[str]:
        sources = self.context().input.get("sources")
        if not isinstance(sources, list):
            raise ContractError("Extraction stage requires a sources list")
        result: list[str] = []
        for item in sources:
            if not isinstance(item, dict) or not str(item.get("source_id") or "").strip():
                raise ContractError("Every extraction source requires a source_id")
            source_id = str(item["source_id"])
            if source_id in result:
                raise ContractError("Extraction source IDs must be unique", source_id=source_id)
            result.append(source_id)
        return result

    def append_draft(
        self,
        artifact: str,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        processed_source_ids: list[str],
    ) -> tuple[dict[str, Any], Path]:
        context = self.context()
        expected = self.expected_source_ids()
        unexpected = sorted(set(processed_source_ids) - set(expected))
        if unexpected:
            raise ContractError("Processed source IDs were not assigned to this extractor", source_ids=unexpected)
        paths = self.paths()
        target = artifact_path(paths, context.attempt_id, artifact)
        current = read_json(target) if target.is_file() else empty_draft()
        if not isinstance(current, dict):
            raise ContractError("Saved extraction draft must be an object")
        merged = merge_draft(current, {"entities": entities, "relations": relations})
        cumulative = list(merged.get("processed_source_ids") or [])
        for source_id in processed_source_ids:
            if source_id not in cumulative:
                cumulative.append(source_id)
        merged["processed_source_ids"] = cumulative
        validate_extraction_draft(
            merged,
            context.input["schema_outline"],
            set(expected),
            require_complete_sources=True,
        )
        if set(cumulative) != set(expected):
            raise ContractError(
                "Extraction completion does not cover every assigned source",
                missing=sorted(set(expected) - set(cumulative)),
            )
        AtomicWriter(paths).json(target, merged)
        return merged, target

    def invalid_batch_payload(self, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, BuilderError):
            context = dict(exc.detail.context)
            message = exc.detail.message
        else:
            context = {}
            message = str(exc)
        context.setdefault("allowed_source_ids", self.expected_source_ids())
        repair_hint = str(context.get("repair_hint") or "").strip()
        if not repair_hint:
            repair_hint = resolve_repair_prompt(
                "extract",
                errors=[message],
                context=context,
            ).strip()
        return {
            "ok": False,
            "error_type": "invalid_instance_batch",
            "error": message,
            "context": context,
            "repair_hint": repair_hint,
            "repair_instruction": (
                "Repair the canonical Instance fields and graph integrity errors, "
                "then call the stage persistence tool again."
            ),
        }
