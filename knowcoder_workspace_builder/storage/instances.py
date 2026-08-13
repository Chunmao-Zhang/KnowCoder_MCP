"""Validate canonical entity and relation Instance records."""

from __future__ import annotations

import math
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .schema import ParsedSchema


def _text(value: Any, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be non-empty text", context=context)
    return value.strip()


def _record_id(value: Any, *, context: str) -> str | int:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ContractError("id must be non-empty text or an integer", context=context)


def _json_value(value: Any, *, context: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("Instance attributes require finite JSON numbers", context=context)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, context=f"{context}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("Instance attribute object keys must be text", context=context)
            _json_value(item, context=f"{context}.{key}")
        return
    raise ContractError("Instance attributes must contain JSON-compatible values", context=context)


def _source_refs(value: Any, *, context: str, allowed_source_ids: set[str] | None) -> list[str]:
    if not isinstance(value, list):
        raise ContractError("source_refs must be a list", context=context)
    refs: list[str] = []
    for item in value:
        ref = _text(item, field="source_refs item", context=context)
        if ref in refs:
            raise ContractError("source_refs must contain unique source IDs", context=context, source_id=ref)
        refs.append(ref)
    if allowed_source_ids is not None:
        unknown = sorted(set(refs) - allowed_source_ids)
        if unknown:
            raise ContractError(
                "Instance record references an unassigned source",
                context=context,
                source_ids=unknown,
                allowed_source_ids=sorted(allowed_source_ids),
            )
    return refs


def _evidence_refs(
    value: Any,
    *,
    context: str,
    source_refs: list[str],
    allowed_chunk_refs: set[tuple[str, str]] | None,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError("evidence_refs must be a list", context=context)
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ContractError("evidence_refs items must be objects", context=context)
        source_id = _text(item.get("source_id"), field="evidence_refs.source_id", context=context)
        chunk_id = _text(item.get("chunk_id"), field="evidence_refs.chunk_id", context=context)
        key = (source_id, chunk_id)
        if source_id not in source_refs:
            raise ContractError("evidence_refs source_id must also appear in source_refs", context=context)
        if allowed_chunk_refs is not None and key not in allowed_chunk_refs:
            raise ContractError("Instance record references an unknown source chunk", context=context, chunk_id=chunk_id)
        if key not in seen:
            refs.append({"source_id": source_id, "chunk_id": chunk_id})
            seen.add(key)
    return refs


def validate_instance_records(
    value: Any,
    *,
    allowed_source_ids: set[str] | None = None,
    allowed_chunk_refs: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate the canonical Instance shape without requiring Schema equality."""
    if not isinstance(value, dict):
        raise ContractError("Instances must be an object")
    entities = value.get("entities")
    relations = value.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ContractError("Instances require entities and relations lists")

    entity_index: set[tuple[str, str]] = set()
    normalized_entities: list[dict[str, Any]] = []
    for position, record in enumerate(entities, start=1):
        context = f"entity {position}"
        if not isinstance(record, dict):
            raise ContractError("Entity record must be an object", position=position)
        entity_type = _text(record.get("type"), field="type", context=context)
        entity_id = _record_id(record.get("id"), context=context)
        name = _text(record.get("name"), field="name", context=context)
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            raise ContractError("Entity attributes must be an object", position=position)
        _json_value(attributes, context=f"{context}.attributes")
        refs = _source_refs(
            record.get("source_refs"),
            context=context,
            allowed_source_ids=allowed_source_ids,
        )
        evidence_refs = _evidence_refs(
            record.get("evidence_refs"),
            context=context,
            source_refs=refs,
            allowed_chunk_refs=allowed_chunk_refs,
        )
        key = (entity_type, str(entity_id))
        if key in entity_index:
            raise ContractError("Duplicate entity record", entity_type=entity_type, entity_id=entity_id)
        entity_index.add(key)
        normalized_entities.append(
            {
                **record,
                "id": entity_id,
                "type": entity_type,
                "name": name,
                "attributes": dict(attributes),
                "source_refs": refs,
                **({"evidence_refs": evidence_refs} if "evidence_refs" in record or evidence_refs else {}),
            }
        )

    relation_index: set[tuple[str, str, str, str, str]] = set()
    normalized_relations: list[dict[str, Any]] = []
    for position, record in enumerate(relations, start=1):
        context = f"relation {position}"
        if not isinstance(record, dict):
            raise ContractError("Relation record must be an object", position=position)
        relation_type = _text(record.get("type"), field="type", context=context)
        head = record.get("head")
        tail = record.get("tail")
        if not isinstance(head, dict) or not isinstance(tail, dict):
            raise ContractError("Relation endpoints must be objects", position=position)
        head_type = _text(head.get("type"), field="head.type", context=context)
        tail_type = _text(tail.get("type"), field="tail.type", context=context)
        head_id = _record_id(head.get("id"), context=f"{context}.head")
        tail_id = _record_id(tail.get("id"), context=f"{context}.tail")
        head_key = (head_type, str(head_id))
        tail_key = (tail_type, str(tail_id))
        if head_key not in entity_index or tail_key not in entity_index:
            raise ContractError(
                "Relation points to a missing entity",
                relation_type=relation_type,
                position=position,
                head={"type": head_type, "id": head_id},
                tail={"type": tail_type, "id": tail_id},
            )
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            raise ContractError("Relation attributes must be an object", position=position)
        _json_value(attributes, context=f"{context}.attributes")
        refs = _source_refs(
            record.get("source_refs"),
            context=context,
            allowed_source_ids=allowed_source_ids,
        )
        evidence_refs = _evidence_refs(
            record.get("evidence_refs"),
            context=context,
            source_refs=refs,
            allowed_chunk_refs=allowed_chunk_refs,
        )
        key = (relation_type, head_type, str(head_id), tail_type, str(tail_id))
        if key in relation_index:
            raise ContractError("Duplicate relation record", relation_type=relation_type, position=position)
        relation_index.add(key)
        normalized_relations.append(
            {
                **record,
                "type": relation_type,
                "head": {**head, "type": head_type, "id": head_id},
                "tail": {**tail, "type": tail_type, "id": tail_id},
                "attributes": dict(attributes),
                "source_refs": refs,
                **({"evidence_refs": evidence_refs} if "evidence_refs" in record or evidence_refs else {}),
            }
        )

    return {
        "format_version": 1,
        "entities": normalized_entities,
        "relations": normalized_relations,
    }


def validate_instances(
    value: Any,
    schema: ParsedSchema,
    *,
    allowed_source_ids: set[str] | None = None,
    allowed_chunk_refs: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Validate public Instance format; Schema remains a separate ontology contract."""
    del schema
    return validate_instance_records(
        value,
        allowed_source_ids=allowed_source_ids,
        allowed_chunk_refs=allowed_chunk_refs,
    )
