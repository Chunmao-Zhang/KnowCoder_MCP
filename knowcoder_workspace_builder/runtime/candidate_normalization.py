"""Explicit mechanical normalization for model-owned semantic candidates."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.workspace_sources import source_records
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.tool_calls import FetchLedger, SearchLedger
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json


def _change(field: str, action: str, detail: str) -> dict[str, str]:
    return {"field": field, "action": action, "detail": detail}


def record_normalization(stage: str, operation: str, changes: list[dict[str, str]]):
    """Append one auditable normalization entry to the active attempt."""
    context = active_invocation_context()
    paths = active_session_paths()
    target = paths.attempts / context.attempt_id / "normalization_log.json"
    if target.is_file():
        loaded = read_json(target)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
            raise ContractError("Normalization log has an invalid structure", path=str(target))
        value = dict(loaded)
    else:
        value = {"format_version": 1, "attempt_id": context.attempt_id, "entries": []}
    entries = list(value["entries"])
    entries.append(
        {
            "sequence": len(entries) + 1,
            "stage": stage,
            "operation": operation,
            "changes": list(changes),
        }
    )
    value["entries"] = entries
    AtomicWriter(paths).json(target, value)
    return target


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be non-empty text")
    return value.strip()


def _text_list(
    value: Any,
    *,
    field: str,
    allow_empty: bool = True,
    deduplicate: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list")
    result = [_text(item, field=f"{field} item") for item in value]
    if not allow_empty and not result:
        raise ContractError(f"{field} cannot be empty")
    if len(result) != len(set(result)) and not deduplicate:
        raise ContractError(f"{field} must contain unique values")
    return list(dict.fromkeys(result)) if deduplicate else result


def normalize_problem_candidate(
    *,
    workspace_action: str,
    base_workspace_id: str | None,
    scope: dict[str, Any],
    steps: list[str],
    missing_information: list[str],
    stage_input: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Bind the original question and the new-workspace ID mechanically."""
    changes: list[dict[str, str]] = []
    action = _text(workspace_action, field="workspace_action").casefold()
    if action not in {"new", "extend"}:
        raise ContractError("workspace_action must be new or extend")
    if not isinstance(scope, dict):
        raise ContractError("scope must be an object")
    normalized_steps = _text_list(steps, field="steps", allow_empty=False)
    normalized_missing = _text_list(
        missing_information,
        field="missing_information",
        deduplicate=True,
    )
    if len(normalized_missing) != len(missing_information):
        changes.append(_change("missing_information", "deduplicated", "Removed repeated items."))
    question = _text(stage_input.get("question"), field="runtime question")
    changes.append(_change("question", "derived", "Copied from the validated stage input."))
    if action == "new":
        if base_workspace_id not in {None, ""}:
            raise ContractError("base_workspace_id must be omitted for a new Workspace")
        base_id = ""
        changes.append(_change("base_workspace_id", "derived", "Set empty for workspace_action=new."))
    else:
        base_id = _text(base_workspace_id, field="base_workspace_id")
    return (
        {
            "workspace_action": action,
            "base_workspace_id": base_id,
            "question": question,
            "scope": dict(scope),
            "steps": normalized_steps,
            "missing_information": normalized_missing,
        },
        changes,
    )


def normalize_schema_judgement_candidate(
    *,
    decision: str,
    missing_requirements: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Canonicalize the judge decision while preserving its semantic boundary."""
    normalized_decision = _text(decision, field="decision").casefold()
    if normalized_decision not in {"pass", "revise"}:
        raise ContractError("Schema judgement decision must be pass or revise")
    normalized_missing = _text_list(
        missing_requirements,
        field="missing_requirements",
        deduplicate=True,
    )
    changes: list[dict[str, str]] = []
    if normalized_decision != decision:
        changes.append(_change("decision", "normalized", "Trimmed and normalized decision casing."))
    if len(normalized_missing) != len(missing_requirements):
        changes.append(_change("missing_requirements", "deduplicated", "Removed repeated items."))
    if normalized_decision == "pass" and normalized_missing:
        normalized_missing = []
        changes.append(
            _change(
                "missing_requirements",
                "cleared",
                "A pass decision is authoritative, so contradictory revision notes were removed.",
            )
        )
    if normalized_decision == "revise" and not normalized_missing:
        raise ContractError("Schema revision requires at least one missing requirement")
    return {
        "decision": normalized_decision,
        "missing_requirements": normalized_missing,
    }, changes


def _accepted_coverage(stage_input: dict[str, Any]) -> list[dict[str, Any]]:
    workspace_context = stage_input.get("workspace_context")
    accepted_data = workspace_context.get("accepted_data") if isinstance(workspace_context, dict) else None
    coverage = accepted_data.get("coverage") if isinstance(accepted_data, dict) else None
    return [dict(item) for item in coverage or [] if isinstance(item, dict)]


def _is_formal_evidence_source(record: dict[str, Any]) -> bool:
    return str(record.get("source_kind") or "").strip() not in {
        "web",
        "web_search",
        "web_search_bundle",
        "web_search_result",
    }


def normalize_evidence_candidate(
    *,
    coverage: list[dict[str, Any]],
    unresolved_gaps: list[str],
    stage_input: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Bind step text, requirements, source IDs, and provenance from runtime state."""
    if not isinstance(coverage, list):
        raise ContractError("Evidence coverage must be a list")
    steps = _text_list(stage_input.get("steps"), field="runtime steps", allow_empty=False)
    gaps = _text_list(unresolved_gaps, field="unresolved_gaps", deduplicate=True)
    changes: list[dict[str, str]] = [
        _change("question", "derived", "Copied from the validated stage input."),
        _change("sources", "derived", "Bound from registered search and upload records."),
    ]
    if len(gaps) != len(unresolved_gaps):
        changes.append(_change("unresolved_gaps", "deduplicated", "Removed repeated items."))

    supplied: dict[int, dict[str, Any]] = {}
    runtime_owned = {"step", "requirements", "source_ids", "chunk_refs", "sources"}
    for position, raw in enumerate(coverage, start=1):
        if not isinstance(raw, dict):
            raise ContractError("Every evidence coverage item must be an object", position=position)
        unknown = set(raw) - {"step_index", "status"} - runtime_owned
        for field in sorted(unknown):
            changes.append(
                _change(
                    f"coverage[{position}].{field}",
                    "ignored",
                    "Ignored a field outside the canonical evidence contract.",
                )
            )
        for field in sorted(set(raw).intersection(runtime_owned)):
            changes.append(
                _change(
                    f"coverage[{position}].{field}",
                    "removed",
                    "Runtime owns this field and rebuilt it from validated context.",
                )
            )
        step_index = raw.get("step_index")
        if not isinstance(step_index, int) or isinstance(step_index, bool) or not 1 <= step_index <= len(steps):
            raise ContractError("Evidence step_index is outside the confirmed step range", position=position)
        status = _text(raw.get("status"), field=f"coverage[{position}].status").casefold()
        if status not in {"covered", "limited", "blocked"}:
            raise ContractError("Evidence coverage status must be covered, limited, or blocked", step_index=step_index)
        if step_index in supplied:
            if supplied[step_index]["status"] != status:
                raise ContractError(
                    "Evidence coverage contains conflicting statuses for one step",
                    step_index=step_index,
                )
            changes.append(
                _change(
                    f"coverage[{position}]",
                    "deduplicated",
                    f"Removed a repeated coverage item for step {step_index}.",
                )
            )
            continue
        supplied[step_index] = {"step_index": step_index, "status": status}

    accepted_by_step = {
        str(item.get("step") or "").strip(): item
        for item in _accepted_coverage(stage_input)
        if str(item.get("step") or "").strip()
    }
    paths = active_session_paths()
    context = active_invocation_context()
    searches = [
        item
        for item in SearchLedger(paths, context.attempt_id).records()
        if item.get("status") == "completed"
    ]
    fetches = [
        item
        for item in FetchLedger(paths, context.attempt_id).records()
        if item.get("status") in {"completed", "partial"}
    ]
    source_ids_by_step: dict[int, set[str]] = {}
    chunk_refs_by_step: dict[int, set[tuple[str, str]]] = {}
    requirements_by_step: dict[int, list[str]] = {}
    for search in [*searches, *fetches]:
        step_index = search.get("step_index")
        response = search.get("response")
        binding = response.get("coverage_binding") if isinstance(response, dict) else None
        ids = binding.get("source_ids") if isinstance(binding, dict) else None
        if not isinstance(step_index, int) or not isinstance(ids, list):
            continue
        source_ids_by_step.setdefault(step_index, set()).update(
            str(source_id).strip() for source_id in ids if str(source_id).strip()
        )
        chunk_refs = binding.get("chunk_refs") if isinstance(binding, dict) else None
        for ref in chunk_refs if isinstance(chunk_refs, list) else []:
            if not isinstance(ref, dict):
                continue
            source_id = str(ref.get("source_id") or "").strip()
            chunk_id = str(ref.get("chunk_id") or "").strip()
            if source_id and chunk_id:
                chunk_refs_by_step.setdefault(step_index, set()).add((source_id, chunk_id))
        requirement = str(search.get("expected_new_information") or search.get("purpose") or "").strip()
        if requirement and requirement not in requirements_by_step.setdefault(step_index, []):
            requirements_by_step[step_index].append(requirement)

    workspace_context = stage_input.get("workspace_context")
    required_source_ids = set(
        _text_list(
            workspace_context.get("required_source_ids") or [],
            field="runtime required_source_ids",
        )
        if isinstance(workspace_context, dict)
        else []
    )
    registered = {
        str(item.get("source_id") or "").strip(): dict(item)
        for item in source_records(paths.root)
        if str(item.get("source_id") or "").strip()
    }
    missing_required = sorted(required_source_ids - set(registered))
    if missing_required:
        raise ContractError("Required evidence sources are not registered", missing_source_ids=missing_required)
    formal_source_ids = {
        source_id for source_id, record in registered.items() if _is_formal_evidence_source(record)
    }

    normalized_coverage: list[dict[str, Any]] = []
    blocking_gaps: list[str] = []
    referenced_source_ids: list[str] = []
    missing_steps: list[int] = []
    for step_index, step in enumerate(steps, start=1):
        accepted = accepted_by_step.get(step)
        semantic = supplied.get(step_index)
        if semantic is None and accepted is None:
            missing_steps.append(step_index)
            continue
        status = semantic["status"] if semantic is not None else str(accepted.get("status") or "").strip()
        if status not in {"covered", "limited", "blocked"}:
            raise ContractError("Accepted evidence has an invalid coverage status", step_index=step_index)
        accepted_ids = {
            str(source_id).strip()
            for source_id in (accepted.get("source_ids") if isinstance(accepted, dict) else []) or []
            if str(source_id).strip() in formal_source_ids
        }
        bound_ids = required_source_ids | accepted_ids | (
            source_ids_by_step.get(step_index, set()) & formal_source_ids
        )
        if status == "covered" and not bound_ids:
            raise ContractError("Covered evidence requires runtime-bound source evidence", step_index=step_index)
        accepted_chunk_refs: set[tuple[str, str]] = set()
        if isinstance(accepted, dict):
            for ref in accepted.get("chunk_refs") or []:
                if not isinstance(ref, dict):
                    continue
                source_id = str(ref.get("source_id") or "").strip()
                chunk_id = str(ref.get("chunk_id") or "").strip()
                if source_id in bound_ids and chunk_id:
                    accepted_chunk_refs.add((source_id, chunk_id))
        bound_chunk_refs = accepted_chunk_refs | {
            ref for ref in chunk_refs_by_step.get(step_index, set()) if ref[0] in bound_ids
        }
        requirements = list(requirements_by_step.get(step_index) or [])
        if isinstance(accepted, dict):
            for requirement in accepted.get("requirements") or []:
                text = str(requirement).strip()
                if text and text not in requirements:
                    requirements.append(text)
        if not requirements:
            requirements = [step]
        item = {
            "step_index": step_index,
            "step": step,
            "requirements": requirements,
            "status": status,
            "source_ids": sorted(bound_ids),
            "chunk_refs": [
                {"source_id": source_id, "chunk_id": chunk_id}
                for source_id, chunk_id in sorted(bound_chunk_refs)
            ],
        }
        normalized_coverage.append(item)
        changes.append(_change(f"coverage[{step_index}].step", "derived", "Copied from confirmed steps."))
        changes.append(
            _change(
                f"coverage[{step_index}].source_ids",
                "derived",
                f"Bound {len(bound_ids)} registered source ID(s).",
            )
        )
        changes.append(
            _change(
                f"coverage[{step_index}].chunk_refs",
                "derived",
                f"Bound {len(bound_chunk_refs)} relevant source chunk(s).",
            )
        )
        changes.append(
            _change(
                f"coverage[{step_index}].requirements",
                "derived",
                "Built from successful search intent, accepted coverage, or the confirmed step.",
            )
        )
        for source_id in sorted(bound_ids):
            if source_id not in referenced_source_ids:
                referenced_source_ids.append(source_id)
        if status == "blocked":
            blocking_gaps.append(f"Step {step_index}: {step}")

    if missing_steps:
        raise ContractError(
            "Evidence coverage is missing steps without an accepted baseline",
            missing_step_indexes=missing_steps,
        )
    missing_registered = sorted(set(referenced_source_ids) - set(registered))
    if missing_registered:
        raise ContractError("Evidence references unregistered runtime sources", source_ids=missing_registered)
    selected_sources = [registered[source_id] for source_id in referenced_source_ids]
    if normalized_coverage and not selected_sources:
        raise ContractError("Evidence-dependent work requires at least one registered source")
    changes.append(_change("blocking_gaps", "derived", "Built from coverage items marked blocked."))
    return (
        {
            "format_version": 2,
            "question": _text(stage_input.get("question"), field="runtime question"),
            "coverage": normalized_coverage,
            "sources": selected_sources,
            "unresolved_gaps": gaps,
            "blocking_gaps": blocking_gaps,
        },
        changes,
    )


def _canonical_value(
    item: dict[str, Any],
    canonical: str,
    aliases: tuple[str, ...],
    *,
    field: str,
    changes: list[dict[str, str]],
) -> Any:
    present = [name for name in (canonical, *aliases) if name in item]
    if not present:
        raise ContractError(f"{field} is required")
    values = [item[name] for name in present]
    if any(value != values[0] for value in values[1:]):
        raise ContractError(f"{field} aliases conflict", aliases=present)
    source = present[0]
    if source != canonical:
        changes.append(_change(field, "renamed", f"Mapped {source} to {canonical}."))
    return values[0]


def _record_id(value: Any, *, field: str) -> str | int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _text(value, field=field)


def normalize_instance_batch(
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    source_ids: list[str],
    evidence_refs: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Canonicalize Instance aliases and inject runtime-owned provenance."""
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ContractError("entities and relations must be lists")
    runtime_source_ids = _text_list(source_ids, field="runtime source_ids", allow_empty=False)
    runtime_evidence_refs: list[dict[str, str]] = []
    allowed_evidence_refs: set[tuple[str, str]] = set()
    for position, ref in enumerate(evidence_refs or [], start=1):
        if not isinstance(ref, dict):
            raise ContractError("Runtime evidence references must be objects", position=position)
        source_id = _text(ref.get("source_id"), field=f"evidence_refs[{position}].source_id")
        chunk_id = _text(ref.get("chunk_id"), field=f"evidence_refs[{position}].chunk_id")
        if source_id not in runtime_source_ids:
            raise ContractError("Runtime evidence reference belongs to an unassigned source", source_id=source_id)
        key = (source_id, chunk_id)
        if key not in allowed_evidence_refs:
            allowed_evidence_refs.add(key)
            runtime_evidence_refs.append({"source_id": source_id, "chunk_id": chunk_id})

    def normalized_record_refs(value: Any, *, field: str) -> list[dict[str, str]]:
        if value is None:
            return list(runtime_evidence_refs)
        if not isinstance(value, list):
            raise ContractError(f"{field} must be a list")
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for position, ref in enumerate(value, start=1):
            if not isinstance(ref, dict):
                raise ContractError(f"{field} items must be objects", position=position)
            source_id = _text(ref.get("source_id"), field=f"{field}[{position}].source_id")
            chunk_id = _text(ref.get("chunk_id"), field=f"{field}[{position}].chunk_id")
            key = (source_id, chunk_id)
            if key not in allowed_evidence_refs:
                raise ContractError(
                    "Instance evidence reference was not supplied to the active extraction unit",
                    source_id=source_id,
                    chunk_id=chunk_id,
                )
            if key not in seen:
                normalized.append({"source_id": source_id, "chunk_id": chunk_id})
                seen.add(key)
        return normalized

    changes: list[dict[str, str]] = []

    def normalized_attributes(item: dict[str, Any], *, field: str) -> dict[str, Any]:
        if "attributes" not in item:
            changes.append(
                _change(
                    field,
                    "derived",
                    "Inserted an empty attributes object because no attribute facts were returned.",
                )
            )
            return {}
        value = item["attributes"]
        if isinstance(value, dict):
            return dict(value)
        changes.append(
            _change(
                field,
                "wrapped",
                "Wrapped a non-object attributes value in the value field.",
            )
        )
        return {"value": value}

    normalized_entities: list[dict[str, Any]] = []
    entity_allowed = {
        "type",
        "entity_type",
        "id",
        "entity_id",
        "name",
        "attributes",
        "source_refs",
        "evidence_refs",
    }
    for position, item in enumerate(entities, start=1):
        if not isinstance(item, dict):
            raise ContractError("Every entity must be an object", position=position)
        unknown = set(item) - entity_allowed
        entity_type = _text(
            _canonical_value(item, "type", ("entity_type",), field=f"entities[{position}].type", changes=changes),
            field=f"entities[{position}].type",
        )
        entity_id = _record_id(
            _canonical_value(item, "id", ("entity_id",), field=f"entities[{position}].id", changes=changes),
            field=f"entities[{position}].id",
        )
        name = _text(item.get("name"), field=f"entities[{position}].name")
        attributes = normalized_attributes(
            item,
            field=f"entities[{position}].attributes",
        )
        for field_name in sorted(unknown):
            if field_name in attributes and attributes[field_name] != item[field_name]:
                raise ContractError(
                    "Entity top-level field conflicts with attributes",
                    position=position,
                    field=field_name,
                )
            attributes[field_name] = item[field_name]
            changes.append(
                _change(
                    f"entities[{position}].{field_name}",
                    "moved",
                    f"Moved unsupported top-level field {field_name} into attributes.",
                )
            )
        if "source_refs" in item:
            changes.append(
                _change(
                    f"entities[{position}].source_refs",
                    "replaced",
                    "Replaced model provenance with all source IDs assigned to this runtime unit.",
                )
            )
        record_evidence_refs = normalized_record_refs(
            item.get("evidence_refs"),
            field=f"entities[{position}].evidence_refs",
        )
        record_source_ids = list(dict.fromkeys(ref["source_id"] for ref in record_evidence_refs))
        normalized_entities.append(
            {
                "type": entity_type,
                "id": entity_id,
                "name": name,
                "attributes": attributes,
                "source_refs": record_source_ids or list(runtime_source_ids),
                "evidence_refs": record_evidence_refs,
            }
        )

    normalized_relations: list[dict[str, Any]] = []
    relation_allowed = {
        "type",
        "relation_type",
        "head",
        "tail",
        "attributes",
        "source_refs",
        "evidence_refs",
    }
    endpoint_allowed = {"type", "entity_type", "id", "entity_id"}
    for position, item in enumerate(relations, start=1):
        if not isinstance(item, dict):
            raise ContractError("Every relation must be an object", position=position)
        unknown = set(item) - relation_allowed
        relation_type = _text(
            _canonical_value(item, "type", ("relation_type",), field=f"relations[{position}].type", changes=changes),
            field=f"relations[{position}].type",
        )
        endpoints: dict[str, dict[str, Any]] = {}
        for endpoint_name in ("head", "tail"):
            endpoint = item.get(endpoint_name)
            if not isinstance(endpoint, dict):
                raise ContractError("Relation endpoints must be objects", position=position, endpoint=endpoint_name)
            endpoint_unknown = set(endpoint) - endpoint_allowed
            for field_name in sorted(endpoint_unknown):
                changes.append(
                    _change(
                        f"relations[{position}].{endpoint_name}.{field_name}",
                        "ignored",
                        "Ignored a field outside the canonical relation endpoint contract.",
                    )
                )
            endpoint_type = _text(
                _canonical_value(
                    endpoint,
                    "type",
                    ("entity_type",),
                    field=f"relations[{position}].{endpoint_name}.type",
                    changes=changes,
                ),
                field=f"relations[{position}].{endpoint_name}.type",
            )
            endpoint_id = _record_id(
                _canonical_value(
                    endpoint,
                    "id",
                    ("entity_id",),
                    field=f"relations[{position}].{endpoint_name}.id",
                    changes=changes,
                ),
                field=f"relations[{position}].{endpoint_name}.id",
            )
            endpoints[endpoint_name] = {"type": endpoint_type, "id": endpoint_id}
        attributes = normalized_attributes(
            item,
            field=f"relations[{position}].attributes",
        )
        for field_name in sorted(unknown):
            if field_name in attributes and attributes[field_name] != item[field_name]:
                raise ContractError(
                    "Relation top-level field conflicts with attributes",
                    position=position,
                    field=field_name,
                )
            attributes[field_name] = item[field_name]
            changes.append(
                _change(
                    f"relations[{position}].{field_name}",
                    "moved",
                    f"Moved unsupported top-level field {field_name} into attributes.",
                )
            )
        if "source_refs" in item:
            changes.append(
                _change(
                    f"relations[{position}].source_refs",
                    "replaced",
                    "Replaced model provenance with all source IDs assigned to this runtime unit.",
                )
            )
        record_evidence_refs = normalized_record_refs(
            item.get("evidence_refs"),
            field=f"relations[{position}].evidence_refs",
        )
        record_source_ids = list(dict.fromkeys(ref["source_id"] for ref in record_evidence_refs))
        normalized_relations.append(
            {
                "type": relation_type,
                "head": endpoints["head"],
                "tail": endpoints["tail"],
                "attributes": attributes,
                "source_refs": record_source_ids or list(runtime_source_ids),
                "evidence_refs": record_evidence_refs,
            }
        )
    changes.append(
        _change(
            "source_refs",
            "derived",
            f"Injected {len(runtime_source_ids)} source ID(s) assigned to the active runtime unit.",
        )
    )
    changes.append(
        _change(
            "evidence_refs",
            "derived",
            f"Bound {len(runtime_evidence_refs)} chunk reference(s) read by the active runtime unit.",
        )
    )
    return normalized_entities, normalized_relations, changes


def schema_blueprint_from_source(source: str) -> dict[str, Any]:
    """Convert accepted Schema source into the semantic blueprint used for patches."""
    parsed = parse_schema(_text(source, field="current_schema"), require_relations=False)
    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for entity in parsed.entities:
        entities.append(
            {
                "name": entity.name,
                "id_type": entity.id_type,
                "description": entity.description,
                "attributes": [
                    {
                        "name": field.name,
                        "type": field.value_type,
                        "optional": field.optional,
                    }
                    for field in entity.attributes
                    if field.name != "name"
                ],
            }
        )
        relations.extend(
            {
                "name": field.name,
                "head": entity.name,
                "tail": field.value_type,
                "many": field.many,
                "optional": field.optional,
                "description": field.description,
            }
            for field in entity.relations
        )
    return {"entities": entities, "relations": relations}


def _schema_alias_text(
    item: dict[str, Any],
    canonical: str,
    aliases: tuple[str, ...],
    *,
    field: str,
    changes: list[dict[str, str]],
) -> str:
    return _text(
        _canonical_value(item, canonical, aliases, field=field, changes=changes),
        field=field,
    )


def _normalize_schema_entity(item: Any, position: int, changes: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ContractError("Every schema entity must be an object", position=position)
    allowed = {"name", "entity_type", "id_type", "entity_data_type", "description", "attributes"}
    unknown = set(item) - allowed
    for field_name in sorted(unknown):
        changes.append(
            _change(
                f"entities[{position}].{field_name}",
                "ignored",
                "Ignored a field outside the canonical Schema entity contract.",
            )
        )
    name = _schema_alias_text(
        item,
        "name",
        ("entity_type",),
        field=f"entities[{position}].name",
        changes=changes,
    )
    id_type = _schema_alias_text(
        item,
        "id_type",
        ("entity_data_type",),
        field=f"entities[{position}].id_type",
        changes=changes,
    )
    description = _text(item.get("description"), field=f"entities[{position}].description")
    attributes = item.get("attributes")
    if not isinstance(attributes, list):
        raise ContractError("Schema entity attributes must be a list", entity=name)
    normalized_attributes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attribute_position, attribute in enumerate(attributes, start=1):
        if not isinstance(attribute, dict):
            raise ContractError("Every schema attribute must be an object", entity=name)
        unknown_attribute = set(attribute) - {"name", "attribute", "type", "attribute_data_type", "optional"}
        for field_name in sorted(unknown_attribute):
            changes.append(
                _change(
                    f"entities[{position}].attributes[{attribute_position}].{field_name}",
                    "ignored",
                    "Ignored a field outside the canonical Schema attribute contract.",
                )
            )
        attribute_name = _schema_alias_text(
            attribute,
            "name",
            ("attribute",),
            field=f"entities[{position}].attributes[{attribute_position}].name",
            changes=changes,
        )
        if attribute_name in seen:
            raise ContractError("Schema entity contains a duplicate attribute", entity=name, field=attribute_name)
        seen.add(attribute_name)
        value_type = _schema_alias_text(
            attribute,
            "type",
            ("attribute_data_type",),
            field=f"entities[{position}].attributes[{attribute_position}].type",
            changes=changes,
        )
        optional = attribute.get("optional")
        if not isinstance(optional, bool):
            raise ContractError("Schema attribute optional must be boolean", entity=name, field=attribute_name)
        normalized_attributes.append({"name": attribute_name, "type": value_type, "optional": optional})
    return {"name": name, "id_type": id_type, "description": description, "attributes": normalized_attributes}


def _normalize_schema_relation(item: Any, position: int, changes: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ContractError("Every schema relation must be an object", position=position)
    allowed = {
        "name",
        "relation_type",
        "head",
        "head_entity_type",
        "tail",
        "tail_entity_type",
        "target",
        "description",
        "many",
        "optional",
    }
    unknown = set(item) - allowed
    for field_name in sorted(unknown):
        changes.append(
            _change(
                f"relations[{position}].{field_name}",
                "ignored",
                "Ignored a field outside the canonical Schema relation contract.",
            )
        )
    name = _schema_alias_text(
        item,
        "name",
        ("relation_type",),
        field=f"relations[{position}].name",
        changes=changes,
    )
    head = _schema_alias_text(
        item,
        "head",
        ("head_entity_type",),
        field=f"relations[{position}].head",
        changes=changes,
    )
    tail = _schema_alias_text(
        item,
        "tail",
        ("tail_entity_type", "target"),
        field=f"relations[{position}].tail",
        changes=changes,
    )
    description = _text(item.get("description"), field=f"relations[{position}].description")
    many = item.get("many")
    optional = item.get("optional")
    if not isinstance(many, bool) or not isinstance(optional, bool):
        raise ContractError("Schema relation many and optional must be boolean", relation=name)
    if many and optional:
        raise ContractError("A multi-value Schema relation cannot also be optional", relation=name)
    return {
        "name": name,
        "head": head,
        "tail": tail,
        "description": description,
        "many": many,
        "optional": optional,
    }


def merge_schema_blueprint(
    current: dict[str, Any],
    *,
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    remove_entity_names: list[str],
    remove_relation_names: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Apply one semantic Schema batch by name without inventing missing values."""
    if not isinstance(current, dict):
        raise ContractError("Current schema blueprint must be an object")
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ContractError("Schema entities and relations must be lists")
    changes: list[dict[str, str]] = []
    remove_entities = _text_list(
        remove_entity_names,
        field="remove_entity_names",
        deduplicate=True,
    )
    remove_relations = _text_list(
        remove_relation_names,
        field="remove_relation_names",
        deduplicate=True,
    )
    if len(remove_entities) != len(remove_entity_names):
        changes.append(_change("remove_entity_names", "deduplicated", "Removed repeated names."))
    if len(remove_relations) != len(remove_relation_names):
        changes.append(_change("remove_relation_names", "deduplicated", "Removed repeated names."))
    if not entities and not relations and not remove_entities and not remove_relations:
        raise ContractError("Schema batch must contain at least one addition, update, or removal")
    incoming_entities = [_normalize_schema_entity(item, index, changes) for index, item in enumerate(entities, start=1)]
    incoming_relations = [
        _normalize_schema_relation(item, index, changes) for index, item in enumerate(relations, start=1)
    ]

    def deduplicate_definitions(
        records: list[dict[str, Any]],
        *,
        kind: str,
    ) -> list[dict[str, Any]]:
        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            name = record["name"]
            existing = unique.get(name)
            if existing is None:
                unique[name] = record
                continue
            if existing != record:
                raise ContractError(f"Schema batch contains conflicting {kind} definitions", name=name)
            changes.append(
                _change(
                    f"{kind}.{name}",
                    "deduplicated",
                    f"Removed an identical repeated {kind} definition.",
                )
            )
        return list(unique.values())

    incoming_entities = deduplicate_definitions(incoming_entities, kind="entities")
    incoming_relations = deduplicate_definitions(incoming_relations, kind="relations")

    entity_map = {
        str(item.get("name") or ""): dict(item)
        for item in current.get("entities") or []
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    relation_map = {
        str(item.get("name") or ""): dict(item)
        for item in current.get("relations") or []
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    missing_entity_removals = sorted(set(remove_entities) - set(entity_map))
    missing_relation_removals = sorted(set(remove_relations) - set(relation_map))
    if missing_entity_removals or missing_relation_removals:
        raise ContractError(
            "Schema removal references names absent from the current blueprint",
            missing_entities=missing_entity_removals,
            missing_relations=missing_relation_removals,
        )
    for name in remove_relations:
        relation_map.pop(name)
        changes.append(_change(f"relations.{name}", "removed", "Applied explicit relation removal."))
    for name in remove_entities:
        entity_map.pop(name)
        changes.append(_change(f"entities.{name}", "removed", "Applied explicit entity removal."))
    for item in incoming_entities:
        action = "updated" if item["name"] in entity_map else "added"
        entity_map[item["name"]] = item
        changes.append(_change(f"entities.{item['name']}", action, "Upserted the complete semantic entity definition."))
    for item in incoming_relations:
        action = "updated" if item["name"] in relation_map else "added"
        relation_map[item["name"]] = item
        changes.append(_change(f"relations.{item['name']}", action, "Upserted the complete semantic relation definition."))
    if not entity_map:
        raise ContractError("Schema blueprint must retain at least one entity")
    return {"entities": list(entity_map.values()), "relations": list(relation_map.values())}, changes
