"""Validate extraction drafts against the canonical Instance file contract."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.storage.instances import validate_instance_records


def schema_allowlist(schema_outline: dict[str, Any]) -> dict[str, Any]:
    """Return Schema names as optional extraction guidance, not validation rules."""
    definitions = schema_outline.get("entities") if isinstance(schema_outline, dict) else None
    if not isinstance(definitions, list):
        return {"entity_types": [], "attributes_by_type": {}, "relations": []}
    entity_types: list[str] = []
    attributes_by_type: dict[str, list[str]] = {}
    relations: list[dict[str, str]] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        entity_type = str(definition.get("entity_type") or "").strip()
        if not entity_type:
            continue
        entity_types.append(entity_type)
        attributes_by_type[entity_type] = sorted(
            {
                str(item.get("name") or "").strip()
                for item in definition.get("attributes") or []
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
        )
        for relation in definition.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            name = str(relation.get("name") or "").strip()
            target = str(relation.get("target") or "").strip()
            if name and target:
                relations.append({"name": name, "head": entity_type, "tail": target})
    return {
        "entity_types": sorted(entity_types),
        "attributes_by_type": attributes_by_type,
        "relations": relations,
    }


def validate_extraction_draft(
    value: Any,
    schema_outline: dict[str, Any],
    expected_source_ids: set[str],
    *,
    require_complete_sources: bool,
    source_text_by_id: dict[str, str] | None = None,
    require_direct_attribute_values: bool = False,
    require_record_source_coverage: bool = False,
) -> dict[str, Any]:
    """Validate only Instance shape plus the current unit's source ownership."""
    del schema_outline, source_text_by_id, require_direct_attribute_values, require_record_source_coverage
    if not isinstance(value, dict):
        raise ContractError("Extraction draft must be an object")
    processed = value.get("processed_source_ids")
    if not isinstance(processed, list):
        raise ContractError("Extraction draft requires processed_source_ids as a list")
    processed_ids: list[str] = []
    for item in processed:
        if not isinstance(item, str) or not item.strip():
            raise ContractError("Extraction processed_source_ids must contain non-empty text")
        source_id = item.strip()
        if source_id in processed_ids:
            raise ContractError("Extraction processed_source_ids must be unique", source_id=source_id)
        processed_ids.append(source_id)
    unexpected = sorted(set(processed_ids) - expected_source_ids)
    if unexpected:
        raise ContractError(
            "Extraction processed an unassigned source",
            source_ids=unexpected,
            allowed_source_ids=sorted(expected_source_ids),
        )
    if require_complete_sources and set(processed_ids) != expected_source_ids:
        raise ContractError(
            "Extraction draft must cover every assigned source",
            missing=sorted(expected_source_ids - set(processed_ids)),
            unexpected=sorted(set(processed_ids) - expected_source_ids),
            allowed_source_ids=sorted(expected_source_ids),
        )
    normalized = validate_instance_records(value, allowed_source_ids=expected_source_ids)
    return {
        **value,
        "entities": normalized["entities"],
        "relations": normalized["relations"],
        "processed_source_ids": processed_ids,
    }
