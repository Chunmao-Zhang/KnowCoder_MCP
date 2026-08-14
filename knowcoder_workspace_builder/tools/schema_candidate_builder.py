"""Parallel question-grounded Schema candidate generation from evidence chunks."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.contracts.errors import (
    ContractError,
    ExternalServiceError,
)
from knowcoder_workspace_builder.runtime.candidate_normalization import (
    merge_schema_blueprint,
)
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_invocation_context,
)
from knowcoder_workspace_builder.runtime.live_events import emit_worker_event
from knowcoder_workspace_builder.runtime.parallel_units import run_parallel_units
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.storage.schema import compile_schema_payload
from knowcoder_workspace_builder.storage.transaction import AtomicWriter

from .source_reader import assigned_schema_chunks
from .unstructured_extractor import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    _client_configuration,
    _integer_setting,
)

WORKERS_ENV = "SCHEMA_BUILD_PARALLEL_WORKERS"
MAX_TOKENS_ENV = "SCHEMA_BUILD_CHUNK_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 1_024

SCHEMA_CANDIDATE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "schema_candidate",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entities", "relations"],
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "id_type", "description", "attributes"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "id_type": {"type": "string", "enum": ["str", "int", "float", "bool"]},
                            "description": {"type": "string", "minLength": 1},
                            "attributes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["name", "type", "optional"],
                                    "properties": {
                                        "name": {"type": "string", "minLength": 1},
                                        "type": {
                                            "type": "string",
                                            "enum": ["str", "int", "float", "bool"],
                                        },
                                        "optional": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                },
                "relations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "head", "tail", "description", "many", "optional"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "head": {"type": "string", "minLength": 1},
                            "tail": {"type": "string", "minLength": 1},
                            "description": {"type": "string", "minLength": 1},
                            "many": {"type": "boolean"},
                            "optional": {"type": "boolean"},
                        },
                    },
                },
            },
        },
    },
}

SCHEMA_CANDIDATE_SYSTEM_PROMPT = """# Role
You design one question-grounded Schema candidate.

# Objective
Convert one source chunk into the reusable entity types, scalar fields, and relations needed to store its relevant facts.

# Input
You receive `question`, all `investigation_steps`, and one `source_chunk`.
Use the question and all steps as the scope. Use the chunk as the only evidence.

# Constraints
- Include only definitions supported by relevant chunk content.
- Cover the entity kinds, measurable properties, events, and connections needed for that content.
- Define reusable types. Do not construct an entity type from an entity's specific name or factual value.
- Use attributes for scalar properties. Use entities for independently identified or repeated records.
- Keep different identities or relation endpoints in different entity types.
- Propose each definition once.
- Set every relation `head` and `tail` to the exact `name` of an entity returned in the same result.
- When a relation needs an endpoint, define its reusable entity type in `entities` before returning the relation.
- Use PascalCase entity names and snake_case field and relation names.
- Name each relation `head_entity_action_tail_entity`. The name must start with its head entity and end with its tail entity in snake_case.
- Keep every relation name different from every attribute name on its head entity.
- Use only `str`, `int`, `float`, or `bool` for ID and attribute types.
- Give each entity and relation one short description.
- Set `optional=false` when a relation has `many=true`.
- Return empty arrays for an unrelated chunk.

# Workflow
1. Select chunk concepts relevant to the complete scope.
2. Define the minimum sufficient reusable entities and scalar fields.
3. Define the relations required to connect them.
4. Collect the returned entity names. Verify that every relation endpoint exactly matches one of them.
5. Verify unique relation names and supported field types.
6. Return the JSON object immediately.

# Output Contract
Return exactly this JSON shape:
{"entities":[{"name":"EntityType","id_type":"str","description":"One short sentence.","attributes":[{"name":"field_name","type":"str","optional":true}]}],"relations":[{"name":"owner_relation_name","head":"HeadEntity","tail":"TailEntity","description":"One short sentence.","many":true,"optional":false}]}

# Examples

## Relevant
Input: A manufacturer adopted a battery technology in 2024. A test measured its energy density at 190 Wh/kg.
Output:
{"entities":[{"name":"Manufacturer","id_type":"str","description":"A manufacturer that adopts a battery technology.","attributes":[]},{"name":"BatteryTechnology","id_type":"str","description":"A battery technology included in the research.","attributes":[]},{"name":"PerformanceMeasurement","id_type":"str","description":"A measured performance result for a battery technology.","attributes":[{"name":"metric_name","type":"str","optional":false},{"name":"value","type":"float","optional":false},{"name":"unit","type":"str","optional":false},{"name":"year","type":"int","optional":true}]}],"relations":[{"name":"manufacturer_adopted_technology","head":"Manufacturer","tail":"BatteryTechnology","description":"Links a manufacturer to an adopted battery technology.","many":true,"optional":false},{"name":"battery_technology_measurements","head":"BatteryTechnology","tail":"PerformanceMeasurement","description":"Links a battery technology to its performance measurements.","many":true,"optional":false}]}

## Unrelated
Input: A museum publishes its weekend opening hours.
Output: {"entities":[],"relations":[]}

# Completion
The result matches the output contract, and every relation endpoint resolves to a returned entity type.
"""


def _parse_candidate(content: str) -> dict[str, list[dict[str, Any]]]:
    try:
        value = json.loads(str(content or "").strip())
    except json.JSONDecodeError as exc:
        raise ContractError("Schema candidate model returned invalid JSON", error=str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("Schema candidate result must be an object")
    entities = value.get("entities")
    relations = value.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        raise ContractError("Schema candidate result requires entities and relations lists")
    if any(not isinstance(item, dict) for item in [*entities, *relations]):
        raise ContractError("Every Schema candidate definition must be an object")
    return {"entities": entities, "relations": relations}


def _build_one(client, model: str, question: str, steps: list[str], chunk: dict[str, Any]) -> dict[str, Any]:
    started_at = time.monotonic()
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=_integer_setting(MAX_TOKENS_ENV, DEFAULT_MAX_TOKENS, minimum=1, maximum=16_384),
        response_format=SCHEMA_CANDIDATE_RESPONSE_FORMAT,
        messages=[
            {"role": "system", "content": SCHEMA_CANDIDATE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "investigation_steps": steps,
                        "source_chunk": chunk["text"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    if not response.choices:
        raise ExternalServiceError("Schema candidate model returned no choices", model=model)
    candidate = _parse_candidate(response.choices[0].message.content or "")
    if candidate["entities"] or candidate["relations"]:
        merge_schema_blueprint(
            {"entities": [], "relations": []},
            entities=candidate["entities"],
            relations=candidate["relations"],
            remove_entity_names=[],
            remove_relation_names=[],
        )
        compile_schema_payload(candidate)
    return {
        **candidate,
        "duration_seconds": round(time.monotonic() - started_at, 3),
    }


def _emit_progress(unit_index: int, unit_total: int, status: str, completed_count: int) -> None:
    emit_worker_event(
        {
            "type": "stage",
            "stage": "schema_build",
            "agent": "schema_builder",
            "status": status,
            "schema_unit_index": unit_index,
            "schema_unit_total": unit_total,
            "schema_completed_count": completed_count,
        }
    )


def _skipped_chunk(index: int, chunk: dict[str, Any], error: BaseException) -> dict[str, Any]:
    return {
        "unit_index": index + 1,
        "source_id": str(chunk.get("source_id") or ""),
        "chunk_id": str(chunk.get("chunk_id") or ""),
        "error_type": type(error).__name__,
        "error": str(error),
    }


def _run_requests(client, model: str, question: str, steps: list[str], chunks: list[dict[str, Any]]):
    worker_count = min(
        len(chunks),
        _integer_setting(WORKERS_ENV, DEFAULT_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    results, skipped = run_parallel_units(
        chunks,
        workers=worker_count,
        operation=lambda chunk: _build_one(client, model, question, steps, chunk),
        describe_error=_skipped_chunk,
        on_progress=_emit_progress,
        stage_name="Schema candidate generation",
    )
    return results, skipped, worker_count


def _preferred(values: list[Any]) -> Any:
    """Choose the most-supported value while preserving first-seen order for ties."""
    counts = Counter(json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values)
    best = max(counts.values())
    chosen = next(value for value in values if counts[json.dumps(value, sort_keys=True, ensure_ascii=False)] == best)
    return chosen


def _provenance_refs(observations: list[tuple[dict[str, Any], dict[str, str]]]) -> list[dict[str, str]]:
    unique = dict.fromkeys(
        (item["source_id"], item["chunk_id"])
        for _record, item in observations
    )
    return [
        {"source_id": source_id, "chunk_id": chunk_id}
        for source_id, chunk_id in unique
    ]


def _merge_candidates(results: dict[int, dict[str, Any]], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    entity_groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    relation_groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for index in sorted(results):
        provenance = {
            "source_id": str(chunks[index].get("source_id") or ""),
            "chunk_id": str(chunks[index].get("chunk_id") or ""),
        }
        for entity in results[index]["entities"]:
            name = str(entity.get("name") or "").strip()
            if not name:
                raise ContractError("Schema candidate entity requires a name", chunk_index=index + 1)
            entity_groups.setdefault(name.casefold(), []).append((entity, provenance))
        for relation in results[index]["relations"]:
            name = str(relation.get("name") or "").strip()
            if not name:
                raise ContractError("Schema candidate relation requires a name", chunk_index=index + 1)
            relation_groups.setdefault(name.casefold(), []).append((relation, provenance))

    conflicts: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, list[dict[str, str]]]] = {"entities": {}, "relations": {}}
    entities: list[dict[str, Any]] = []
    for observations in entity_groups.values():
        records = [record for record, _provenance in observations]
        attributes_by_name: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            attributes = record.get("attributes")
            if not isinstance(attributes, list):
                raise ContractError("Schema candidate entity attributes must be a list", entity=record.get("name"))
            for attribute in attributes:
                if not isinstance(attribute, dict) or not str(attribute.get("name") or "").strip():
                    raise ContractError("Schema candidate attribute requires a name", entity=record.get("name"))
                attributes_by_name.setdefault(str(attribute["name"]).strip().casefold(), []).append(attribute)
        merged_attributes: list[dict[str, Any]] = []
        for attribute_records in attributes_by_name.values():
            chosen = dict(_preferred(attribute_records))
            merged_attributes.append(chosen)
            signatures = {(item.get("type"), item.get("optional")) for item in attribute_records}
            if len(signatures) > 1:
                conflicts.append(
                    {
                        "kind": "attribute",
                        "entity": str(records[0].get("name") or ""),
                        "name": str(chosen.get("name") or ""),
                        "observed_signatures": [list(item) for item in sorted(signatures, key=str)],
                    }
                )
        names = [str(record.get("name") or "").strip() for record in records]
        id_types = [record.get("id_type") for record in records]
        description = next(
            (str(record.get("description") or "").strip() for record in records if str(record.get("description") or "").strip()),
            "",
        )
        entities.append(
            {
                "name": _preferred(names),
                "id_type": _preferred(id_types),
                "description": description,
                "attributes": merged_attributes,
            }
        )
        provenance["entities"][entities[-1]["name"]] = _provenance_refs(observations)
        if len(set(map(str, id_types))) > 1:
            conflicts.append({"kind": "entity_id_type", "name": entities[-1]["name"], "observed": id_types})

    relations: list[dict[str, Any]] = []
    canonical_entity_names = {
        str(entity.get("name") or "").casefold(): str(entity.get("name") or "")
        for entity in entities
        if str(entity.get("name") or "").strip()
    }
    for observations in relation_groups.values():
        records = [record for record, _provenance in observations]
        chosen = dict(_preferred(records))
        for endpoint in ("head", "tail"):
            value = str(chosen.get(endpoint) or "").strip()
            canonical = canonical_entity_names.get(value.casefold())
            if not canonical:
                raise ContractError(
                    "Merged Schema relation references an unknown entity",
                    relation=chosen.get("name"),
                    endpoint=endpoint,
                    entity=value,
                )
            chosen[endpoint] = canonical
        chosen["description"] = next(
            (str(record.get("description") or "").strip() for record in records if str(record.get("description") or "").strip()),
            "",
        )
        relations.append(chosen)
        provenance["relations"][str(chosen.get("name") or "")] = _provenance_refs(observations)
        signatures = {(item.get("head"), item.get("tail"), item.get("many"), item.get("optional")) for item in records}
        if len(signatures) > 1:
            conflicts.append(
                {
                    "kind": "relation",
                    "name": str(chosen.get("name") or ""),
                    "observed_signatures": [list(item) for item in sorted(signatures, key=str)],
                }
            )
    return {"entities": entities, "relations": relations, "conflicts": conflicts, "provenance": provenance}


@tool
def build_schema_candidates() -> str:
    """Build and merge question-grounded Schema candidates from every assigned evidence chunk."""
    context = active_invocation_context()
    if context.stage != "schema_build":
        raise ContractError("build_schema_candidates can run only during the schema_build stage")
    question = str(context.input.get("question") or "").strip()
    steps = [str(item).strip() for item in context.input.get("steps") or [] if str(item).strip()]
    if not question or not steps:
        raise ContractError("Schema candidate generation requires the complete question and investigation steps")
    chunks = assigned_schema_chunks()
    client, model = _client_configuration()
    results, skipped, worker_count = _run_requests(client, model, question, steps, chunks)
    merged = _merge_candidates(results, chunks)
    paths = active_session_paths()
    target = paths.attempts / context.attempt_id / "schema_candidates.json"
    AtomicWriter(paths).json(
        target,
        {
            "format_version": 1,
            "model": model,
            "chunk_count": len(chunks),
            "successful_chunk_count": len(results),
            "skipped_chunk_count": len(skipped),
            "skipped_chunks": [skipped[index] for index in sorted(skipped)],
            "concurrency": worker_count,
            "request_durations_seconds": {
                str(index + 1): float(result.get("duration_seconds") or 0)
                for index, result in sorted(results.items())
            },
            **merged,
        },
    )
    return json.dumps(
        {
            "ok": True,
            "chunk_count": len(chunks),
            "successful_chunk_count": len(results),
            "skipped_chunk_count": len(skipped),
            "entity_count": len(merged["entities"]),
            "relation_count": len(merged["relations"]),
            "candidate_schema": {
                "entities": merged["entities"],
                "relations": merged["relations"],
            },
            "conflicts": merged["conflicts"],
            "provenance_path": target.relative_to(paths.root).as_posix(),
        },
        ensure_ascii=False,
    )
