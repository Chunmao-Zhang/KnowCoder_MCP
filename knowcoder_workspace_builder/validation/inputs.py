"""Stage input validation declared from one shared protocol table."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .stage_results import STAGE_PROTOCOLS

INPUT_FIELDS: dict[str, tuple[str, ...]] = {
    stage: protocol.input_fields for stage, protocol in STAGE_PROTOCOLS.items()
}


def validate_stage_input(stage: str, value: Any) -> dict[str, Any]:
    required = INPUT_FIELDS.get(stage)
    if required is None:
        raise ContractError("Unknown stage input contract", stage=stage)
    if not isinstance(value, dict):
        raise ContractError("Stage input must be an object", stage=stage)
    missing = [field for field in required if field not in value]
    if missing:
        raise ContractError("Stage input is missing required fields", stage=stage, missing=missing)
    if stage in {"problem", "evidence", "schema_build", "schema_judge"} and not str(value.get("question") or "").strip():
        raise ContractError("Stage input question cannot be empty", stage=stage)
    if stage in {"problem", "evidence"}:
        upload_paths = value.get("upload_paths")
        if not isinstance(upload_paths, list) or any(not isinstance(item, str) or not item.strip() for item in upload_paths):
            raise ContractError("upload_paths must be a list of non-empty paths", stage=stage)
    if "workspace_context" in required and not isinstance(value.get("workspace_context"), dict):
        raise ContractError("workspace_context must be an object", stage=stage)
    if stage in {"evidence", "schema_build", "schema_judge"}:
        steps = value.get("steps")
        if not isinstance(steps, list) or not steps or any(not str(item).strip() for item in steps):
            raise ContractError("steps must be a non-empty list of text", stage=stage)
    if stage == "evidence":
        if not str(value.get("research_dir") or "").strip():
            raise ContractError("research_dir cannot be empty", stage=stage)
    if stage in {"schema_build", "schema_judge"} and not isinstance(value.get("data_manifest"), dict):
        raise ContractError("data_manifest must be an object", stage=stage)
    if stage == "schema_judge" and not str(value.get("schema_source") or "").strip():
        raise ContractError("schema_source cannot be empty", stage=stage)
    if stage in {"extract", "structured_extract"}:
        if not isinstance(value.get("schema_outline"), dict):
            raise ContractError("schema_outline must be an object", stage=stage)
        if not str(value.get("draft_path") or "").strip():
            raise ContractError("draft_path cannot be empty", stage=stage)
        if stage == "structured_extract":
            if not str(value.get("work_dir") or "").strip():
                raise ContractError("work_dir cannot be empty", stage=stage)
            if not str(value.get("batch_path") or "").strip():
                raise ContractError("batch_path cannot be empty", stage=stage)
        sources = value.get("sources")
        if not isinstance(sources, list):
            raise ContractError("Extractor sources must be a list", stage=stage)
        source_ids: list[str] = []
        for source in sources:
            if not isinstance(source, dict) or not str(source.get("source_id") or "").strip():
                raise ContractError("Every extraction source requires a source_id", stage=stage)
            source_ids.append(str(source["source_id"]))
        if len(source_ids) != len(set(source_ids)):
            raise ContractError("Extraction source IDs must be unique", stage=stage)
    if stage == "document":
        if not isinstance(value.get("problem"), dict):
            raise ContractError("Document input problem must be an object", stage=stage)
        if not str(value.get("schema_source") or "").strip():
            raise ContractError("Document input schema_source cannot be empty", stage=stage)
        summary = value.get("instance_summary")
        if not isinstance(summary, dict):
            raise ContractError("Document input instance_summary must be an object", stage=stage)
        for field in ("entity_count", "relation_count"):
            count = summary.get(field)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ContractError(f"Document input {field} must be a non-negative integer", stage=stage)
        if not isinstance(value.get("sources"), list):
            raise ContractError("Document input sources must be a list", stage=stage)
        if not isinstance(value.get("artifact_index"), dict):
            raise ContractError("Document input artifact_index must be an object", stage=stage)
    return dict(value)
