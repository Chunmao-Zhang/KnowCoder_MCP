"""Attempt-owned Agent artifacts and instance draft merging."""

from __future__ import annotations

import hashlib
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .paths import SessionPaths
from .transaction import AtomicWriter


def artifact_path(paths: SessionPaths, attempt_id: str, name: str, suffix: str = ".json") -> Path:
    if not name.replace("_", "").isalnum():
        raise ContractError("Artifact name is invalid", artifact=name)
    return paths.attempts / attempt_id / f"{name}{suffix}"


def write_artifact(
    paths: SessionPaths,
    attempt_id: str,
    name: str,
    value: Any,
    *,
    suffix: str = ".json",
) -> Path:
    target = artifact_path(paths, attempt_id, name, suffix)
    writer = AtomicWriter(paths)
    return writer.text(target, str(value)) if suffix != ".json" else writer.json(target, value)


def empty_draft() -> dict[str, Any]:
    return {"format_version": 1, "processed_source_ids": [], "entities": [], "relations": []}


def _normalize_identity_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return " ".join(text.split())


def _entity_identity_key(item: dict[str, Any]) -> tuple[str, str]:
    entity_type = str(item.get("type") or item.get("entity_type") or "")
    identity_name = _normalize_identity_name(item.get("name"))
    return entity_type, identity_name


def _values_equivalent(left: Any, right: Any) -> bool:
    if left == right:
        return True
    left_text = str(left or "").strip().casefold()
    right_text = str(right or "").strip().casefold()
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    aliases = {
        frozenset({"msa", "metropolitan statistical area", "metro", "metro area"}),
        frozenset({"city", "municipality"}),
        frozenset({"county", "counties"}),
        frozenset({"neighborhood", "district", "area"}),
        frozenset({"corridor", "highway corridor"}),
        frozenset({"country", "nation"}),
    }
    for group in aliases:
        if left_text in group and right_text in group:
            return True
    try:
        left_num = Decimal(left_text.replace(",", "").replace("$", "").replace("%", ""))
        right_num = Decimal(right_text.replace(",", "").replace("$", "").replace("%", ""))
        return left_num == right_num
    except (InvalidOperation, ValueError):
        return False


def _prefer_attribute_value(field: str, current: Any, incoming: Any) -> Any:
    current_text = str(current or "").strip()
    incoming_text = str(incoming or "").strip()
    if not current_text:
        return incoming
    if not incoming_text:
        return current
    if _values_equivalent(current, incoming):
        return current if len(current_text) >= len(incoming_text) else incoming
    if field in {"description", "statement", "text_value", "source"}:
        return current if len(current_text) >= len(incoming_text) else incoming
    # Keep first-seen value for hard disagreements; source_refs still merge.
    return current


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _record_id(value: dict[str, Any], primary: str, alternate: str) -> str:
    raw = value[primary] if primary in value else value.get(alternate, "")
    return str(raw)


def _disambiguated_entity_id(entity_type: str, identity_name: str) -> str:
    """Build a stable ID when a model reuses one ID for different named entities."""
    identity = "\0".join((entity_type.casefold(), identity_name))
    return f"entity-{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _merge_record(
    current: dict[str, Any],
    incoming: dict[str, Any],
    *,
    prefer_incoming_conflicts: bool = False,
) -> dict[str, Any]:
    merged = dict(current)
    current_attributes = current.get("attributes") or {}
    incoming_attributes = incoming.get("attributes") or {}
    if not isinstance(current_attributes, dict) or not isinstance(incoming_attributes, dict):
        raise ContractError("Instance record attributes must be objects")
    attributes = dict(current_attributes)
    for field, value in incoming_attributes.items():
        if field not in attributes:
            attributes[field] = value
            continue
        if prefer_incoming_conflicts and _has_meaningful_value(value):
            attributes[field] = value
            continue
        if attributes[field] == value or _values_equivalent(attributes[field], value):
            attributes[field] = _prefer_attribute_value(field, attributes[field], value)
            continue
        attributes[field] = _prefer_attribute_value(field, attributes[field], value)
    merged["attributes"] = attributes
    current_name = str(current.get("name") or "").strip()
    incoming_name = str(incoming.get("name") or "").strip()
    if incoming_name and (
        not current_name
        or (
            prefer_incoming_conflicts
            and _normalize_identity_name(current_name) == _normalize_identity_name(incoming_name)
        )
        or (
            _normalize_identity_name(current_name) == _normalize_identity_name(incoming_name)
            and len(incoming_name) < len(current_name)
        )
    ):
        merged["name"] = incoming_name
    source_refs = [str(item) for item in current.get("source_refs") or []]
    for item in incoming.get("source_refs") or []:
        normalized = str(item)
        if normalized and normalized not in source_refs:
            source_refs.append(normalized)
    merged["source_refs"] = source_refs
    evidence_refs = [dict(item) for item in current.get("evidence_refs") or [] if isinstance(item, dict)]
    seen_evidence = {
        (str(item.get("source_id") or ""), str(item.get("chunk_id") or ""))
        for item in evidence_refs
    }
    for item in incoming.get("evidence_refs") or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("source_id") or ""), str(item.get("chunk_id") or ""))
        if all(key) and key not in seen_evidence:
            evidence_refs.append({"source_id": key[0], "chunk_id": key[1]})
            seen_evidence.add(key)
    merged["evidence_refs"] = evidence_refs
    return merged


def _relation_key(value: dict[str, Any]) -> tuple[str, str, str, str, str]:
    head = value.get("head") if isinstance(value.get("head"), dict) else {}
    tail = value.get("tail") if isinstance(value.get("tail"), dict) else {}
    return (
        str(value.get("type") or value.get("relation_type") or ""),
        str(head.get("type") or ""),
        _record_id(head, "id", "entity_id"),
        str(tail.get("type") or ""),
        _record_id(tail, "id", "entity_id"),
    )


def merge_draft(
    current: dict[str, Any],
    batch: dict[str, Any],
    *,
    prefer_incoming_conflicts: bool = False,
) -> dict[str, Any]:
    """Merge one extraction batch into the cumulative draft.

    Entity identity is based on type and normalized name. Model IDs are preserved
    when unambiguous. A reused ID with a different name receives a deterministic
    replacement, and relation endpoints from that batch are rewritten.
    """
    if not isinstance(batch, dict):
        raise ContractError("Instance batch must be an object")
    entities = batch.get("entities", [])
    relations = batch.get("relations", [])
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ContractError("Instance batch entities and relations must be lists")

    merged = {
        "format_version": 1,
        "processed_source_ids": list(current.get("processed_source_ids") or []),
        "entities": list(current.get("entities") or []),
        "relations": list(current.get("relations") or []),
    }
    entity_index = {
        (str(item.get("type") or item.get("entity_type") or ""), _record_id(item, "id", "entity_id")): position
        for position, item in enumerate(merged["entities"])
        if isinstance(item, dict)
    }
    name_index: dict[tuple[str, str], str] = {}
    for item in merged["entities"]:
        if not isinstance(item, dict):
            continue
        entity_type, identity_name = _entity_identity_key(item)
        entity_id = _record_id(item, "id", "entity_id")
        if entity_type and identity_name:
            name_index[(entity_type, identity_name)] = entity_id
    id_aliases: dict[tuple[str, str], str] = {}

    for item in entities:
        if not isinstance(item, dict):
            raise ContractError("Every entity batch record must be an object")
        entity_type = str(item.get("type") or item.get("entity_type") or "")
        entity_id = _record_id(item, "id", "entity_id")
        if not entity_type or not entity_id:
            raise ContractError("Entity batch record requires type and id")
        _, identity_name = _entity_identity_key(item)
        canonical_id = entity_id
        identity_key = (entity_type, identity_name)
        if identity_name and identity_key in name_index:
            canonical_id = name_index[identity_key]
            if canonical_id != entity_id:
                id_aliases[(entity_type, entity_id)] = canonical_id
                item = {**item, "id": canonical_id}
        key = (entity_type, canonical_id)
        if key in entity_index:
            existing = merged["entities"][entity_index[key]]
            existing_identity = _entity_identity_key(existing)
            if identity_name and existing_identity != identity_key:
                canonical_id = _disambiguated_entity_id(entity_type, identity_name)
                key = (entity_type, canonical_id)
                id_aliases[(entity_type, entity_id)] = canonical_id
                item = {**item, "id": canonical_id}
        if key in entity_index:
            merged["entities"][entity_index[key]] = _merge_record(
                merged["entities"][entity_index[key]],
                item,
                prefer_incoming_conflicts=prefer_incoming_conflicts,
            )
        else:
            entity_index[key] = len(merged["entities"])
            merged["entities"].append(dict(item))
            if identity_name:
                name_index[identity_key] = canonical_id

    relation_index = {
        _relation_key(item): position for position, item in enumerate(merged["relations"]) if isinstance(item, dict)
    }
    for item in relations:
        if not isinstance(item, dict):
            raise ContractError("Every relation batch record must be an object")
        rewritten = dict(item)
        for endpoint_key in ("head", "tail"):
            endpoint = rewritten.get(endpoint_key)
            if not isinstance(endpoint, dict):
                continue
            endpoint_type = str(endpoint.get("type") or "")
            endpoint_id = _record_id(endpoint, "id", "entity_id")
            alias = id_aliases.get((endpoint_type, endpoint_id))
            if alias:
                rewritten[endpoint_key] = {**endpoint, "id": alias, "type": endpoint_type}
        key = _relation_key(rewritten)
        if not all(key):
            raise ContractError("Relation batch record requires type and complete endpoints")
        if key in relation_index:
            merged["relations"][relation_index[key]] = _merge_record(
                merged["relations"][relation_index[key]],
                rewritten,
                prefer_incoming_conflicts=prefer_incoming_conflicts,
            )
        else:
            relation_index[key] = len(merged["relations"])
            merged["relations"].append(rewritten)
    return merged


def merge_final_drafts(drafts: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge accepted drafts in authority order; later stages replace stale facts."""
    merged = empty_draft()
    for draft in drafts:
        merged = merge_draft(merged, draft, prefer_incoming_conflicts=True)
        for source_id in draft.get("processed_source_ids") or []:
            normalized = str(source_id).strip()
            if normalized and normalized not in merged["processed_source_ids"]:
                merged["processed_source_ids"].append(normalized)
    return {
        "format_version": 1,
        "entities": merged["entities"],
        "relations": merged["relations"],
    }
