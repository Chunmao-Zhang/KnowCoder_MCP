"""Batch unstructured extraction backed by one model request per assigned chunk."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import httpx
from langchain_core.tools import tool
from openai import OpenAI

from knowcoder_workspace_builder.contracts.errors import (
    ContractError,
    ExternalServiceError,
)
from knowcoder_workspace_builder.runtime.candidate_normalization import (
    normalize_instance_batch,
)
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_invocation_context,
)
from knowcoder_workspace_builder.runtime.live_events import emit_worker_event
from knowcoder_workspace_builder.runtime.parallel_units import run_parallel_units
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_session_path
from knowcoder_workspace_builder.storage.stage_artifacts import empty_draft, merge_draft
from knowcoder_workspace_builder.storage.transaction import AtomicWriter
from knowcoder_workspace_builder.validation.extraction import validate_extraction_draft

from .source_reader import assigned_extraction_chunks

API_KEY_ENV = "UNSTRUCTURED_EXTRACTION_API_KEY"
BASE_URL_ENV = "UNSTRUCTURED_EXTRACTION_BASE_URL"
MODEL_ENV = "UNSTRUCTURED_EXTRACTION_MODEL"
TIMEOUT_ENV = "UNSTRUCTURED_EXTRACTION_TIMEOUT_SECONDS"
MAX_TOKENS_ENV = "UNSTRUCTURED_EXTRACTION_MAX_TOKENS"
WORKERS_ENV = "SCHEMA_EXTRACT_PARALLEL_WORKERS"

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_TOKENS = 4_096
DEFAULT_WORKERS = 25
MAX_WORKERS = 32

ExtractionProgressSink = Callable[[int, int, str, int], None]

EXTRACTION_SYSTEM_PROMPT = """# Role
You are an unstructured information extractor.

# Objective
Extract every entity and relation in one source chunk that fits the supplied Schema outline.

# Input
You receive one `schema_outline` and one `content` chunk.
Treat `content` as the complete factual boundary for this request.
Treat `schema_outline` as the set of allowed types and fields, not a form that must be filled.

# Constraints
- Create an entity only when the chunk states its identity and at least one factual property required by its type.
- Include an attribute only when its value is explicit in the chunk.
- Copy numbers, units, dates, ranges, names, identifiers, and URLs from the chunk.
- Preserve qualifications and the scope attached to every value.
- Keep unsupported fields absent. Keep unsupported entities and relations absent.
- Use empty arrays when the chunk supplies no complete relevant record.
- Exclude placeholder values, example URLs, empty strings, zero substitutes, memory, inference, and arithmetic estimates.
- Preserve numbers, units, dates, ranges, scope, and qualifications.
- Use the Schema outline to determine the target entity types, attributes, and relations.
- Create stable, readable IDs within each entity type.
- Include every relation endpoint in entities with the same type and ID.
- Verify every relation endpoint resolves to an entity in the response.
- Represent attributes as JSON objects.
- Put factual details in attributes using JSON-compatible values.
- Return strict JSON with only entities and relations.

# Workflow
1. Locate explicit facts in the chunk.
2. Match those facts to allowed Schema types and fields.
3. Create only records whose identity and values are supported by the same chunk.
4. Verify every returned attribute value against the chunk text.
5. Return the JSON object immediately.

# Output Contract
Each entity contains type, id, name, and attributes.
Each relation contains type, head, tail, and attributes.
Relation head and tail are objects containing type and id.

# Examples

## Explicit measurement
Content: The device generated 30 tokens per second on an iPhone 15 Pro.
Output: {"entities":[{"type":"DevicePerformanceMeasurement","id":"iphone-15-pro-generation-speed","name":"iPhone 15 Pro generation speed","attributes":{"device":"iPhone 15 Pro","metric_name":"generation speed","metric_value":30,"unit":"tokens per second"}}],"relations":[]}

## Qualitative statement
Content: Production quantization meets the device memory requirement. The report gives no memory value.
Output: {"entities":[],"relations":[]}

# Completion
Every returned identity and attribute value is directly supported by the input chunk.
"""


def _remove_relations_with_missing_endpoints(
    draft: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Keep valid extracted facts while auditing relations with unresolved endpoints."""
    entity_keys = {
        (str(item.get("type") or ""), str(item.get("id") or ""))
        for item in draft.get("entities") or []
        if isinstance(item, dict)
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for position, relation in enumerate(draft.get("relations") or [], start=1):
        if not isinstance(relation, dict):
            accepted.append(relation)
            continue
        head = relation.get("head")
        tail = relation.get("tail")
        if not isinstance(head, dict) or not isinstance(tail, dict):
            accepted.append(relation)
            continue
        missing = []
        for endpoint_name, endpoint in (("head", head), ("tail", tail)):
            key = (str(endpoint.get("type") or ""), str(endpoint.get("id") or ""))
            if all(key) and key not in entity_keys:
                missing.append({"endpoint": endpoint_name, "type": key[0], "id": key[1]})
        if not missing:
            accepted.append(relation)
            continue
        rejected.append(
            {
                "position": position,
                "reason": "relation_endpoint_missing",
                "missing_endpoints": missing,
                "relation": relation,
            }
        )
    return {**draft, "relations": accepted}, rejected


def _integer_setting(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name) or default).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _timeout_setting() -> float:
    raw = str(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_SECONDS).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{TIMEOUT_ENV} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{TIMEOUT_ENV} must be positive")
    return value


def _client_configuration() -> tuple[OpenAI, str]:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    base_url = os.environ.get(BASE_URL_ENV, "").strip().rstrip("/")
    model = os.environ.get(MODEL_ENV, "").strip()
    missing = [
        name
        for name, value in ((API_KEY_ENV, api_key), (BASE_URL_ENV, base_url), (MODEL_ENV, model))
        if not value
    ]
    if missing:
        raise ExternalServiceError(
            "Unstructured extraction API configuration is incomplete",
            missing_environment=missing,
        )
    request_timeout = _timeout_setting()
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=httpx.Timeout(request_timeout, connect=min(request_timeout, DEFAULT_CONNECT_TIMEOUT_SECONDS)),
        # Retry only in the batch runner so every failed attempt is visible and bounded.
        max_retries=0,
    ), model


def _json_object(content: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    text = str(content or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
            if text.casefold().startswith("json\n"):
                text = text[5:].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("Extraction model returned invalid JSON", error=str(exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("Extraction model result must be an object")
    if not isinstance(value.get("entities"), list) or not isinstance(value.get("relations"), list):
        raise ContractError("Extraction model result requires entities and relations lists")
    ignored = [
        {
            "field": field,
            "action": "ignored",
            "detail": "Ignored a field outside the canonical extraction result contract.",
        }
        for field in sorted(set(value) - {"entities", "relations"})
    ]
    return {"entities": value["entities"], "relations": value["relations"]}, ignored


def _extract_one(
    client: OpenAI,
    model: str,
    schema_outline: dict[str, Any],
    chunk: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_outline": schema_outline,
        "content": chunk["text"],
    }
    evidence_chunk_id = str(chunk.get("evidence_chunk_id") or chunk.get("chunk_id") or "")
    evidence_refs = [{"source_id": chunk["source_id"], "chunk_id": evidence_chunk_id}] if evidence_chunk_id else []
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=_integer_setting(MAX_TOKENS_ENV, DEFAULT_MAX_TOKENS, minimum=1, maximum=16_384),
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    if not response.choices:
        raise ExternalServiceError("Extraction model returned no choices", model=model)
    batch, parse_changes = _json_object(response.choices[0].message.content or "")
    entities, relations, changes = normalize_instance_batch(
        entities=batch["entities"],
        relations=batch["relations"],
        source_ids=[chunk["source_id"]],
        evidence_refs=evidence_refs,
    )
    return {
        "entities": entities,
        "relations": relations,
        "normalization_changes": [
            change
            for change in [*parse_changes, *changes]
            if change.get("action") in {"derived", "ignored", "moved", "wrapped"}
        ],
    }


def _emit_extraction_progress(unit_index: int, unit_total: int, status: str, completed_count: int) -> None:
    """Publish real per-chunk lifecycle updates for the extraction progress bar."""
    emit_worker_event(
        {
            "type": "stage",
            "stage": "extract",
            "agent": "data_extractor",
            "status": status,
            "extract_unit_index": unit_index,
            "extract_unit_total": unit_total,
            "extract_completed_count": completed_count,
        }
    )


def _run_chunk_requests(
    client: OpenAI,
    model: str,
    schema_outline: dict[str, Any],
    chunks: list[dict[str, Any]],
    *,
    on_progress: ExtractionProgressSink | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], int]:
    max_workers = min(
        len(chunks),
        _integer_setting(WORKERS_ENV, DEFAULT_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    results, skipped = run_parallel_units(
        chunks,
        workers=max_workers,
        operation=lambda chunk: _extract_one(client, model, schema_outline, chunk),
        describe_error=lambda index, chunk, error: {
            "unit_index": index + 1,
            "source_id": str(chunk.get("source_id") or ""),
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "error_type": type(error).__name__,
            "error": str(error),
        },
        on_progress=on_progress,
        stage_name="Unstructured extraction",
    )
    return results, skipped, max_workers


@tool
def extract_unstructured_chunks() -> str:
    """Extract all chunks assigned to the current stage and persist one validated draft."""
    context = active_invocation_context()
    if context.stage != "extract":
        raise ContractError("extract_unstructured_chunks can run only during the extract stage")
    chunks = assigned_extraction_chunks()
    schema_outline = context.input.get("schema_outline")
    if not isinstance(schema_outline, dict):
        raise ContractError("extract stage input requires schema_outline")
    client, model = _client_configuration()
    worker_count = min(
        len(chunks),
        _integer_setting(WORKERS_ENV, DEFAULT_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    results, skipped, worker_count = _run_chunk_requests(
        client,
        model,
        schema_outline,
        chunks,
        on_progress=_emit_extraction_progress,
    )

    draft = empty_draft()
    for index in sorted(results):
        draft = merge_draft(draft, results[index])
    normalization_entries = [
        {
            "unit_index": index + 1,
            "source_id": chunks[index]["source_id"],
            "chunk_id": chunks[index]["chunk_id"],
            "changes": list(results[index].get("normalization_changes") or []),
        }
        for index in sorted(results)
        if results[index].get("normalization_changes")
    ]
    processed_source_ids = list(
        dict.fromkeys(
            source_id
            for item in context.input.get("sources") or []
            if isinstance(item, dict) and (source_id := str(item.get("source_id") or "").strip())
        )
    )
    draft["processed_source_ids"] = processed_source_ids
    draft, rejected_relations = _remove_relations_with_missing_endpoints(draft)

    paths = active_session_paths()
    rejected_path = paths.attempts / context.attempt_id / "rejected_relations.json"
    AtomicWriter(paths).json(
        rejected_path,
        {
            "format_version": 1,
            "count": len(rejected_relations),
            "items": rejected_relations,
        },
    )
    AtomicWriter(paths).json(
        paths.attempts / context.attempt_id / "extraction_normalization_log.json",
        {
            "format_version": 1,
            "count": sum(len(item["changes"]) for item in normalization_entries),
            "units": normalization_entries,
        },
    )
    validated = validate_extraction_draft(
        draft,
        schema_outline,
        set(processed_source_ids),
        require_complete_sources=True,
    )

    draft_path = paths.attempts / context.attempt_id / "unstructured_draft.json"
    AtomicWriter(paths).json(draft_path, validated)
    summary = {
        "format_version": 1,
        "model": model,
        "chunk_count": len(chunks),
        "successful_chunks": len(results),
        "skipped_chunk_count": len(skipped),
        "skipped_chunks": [skipped[index] for index in sorted(skipped)],
        "concurrency": worker_count,
        "entity_count": len(validated["entities"]),
        "relation_count": len(validated["relations"]),
        "rejected_relation_count": len(rejected_relations),
        "normalization_change_count": sum(len(item["changes"]) for item in normalization_entries),
    }
    AtomicWriter(paths).json(paths.attempts / context.attempt_id / "extract_unit_results.json", summary)
    return json.dumps(
        {
            "ok": True,
            "processed_chunks": len(results),
            "skipped_chunks": len(skipped),
            "entity_count": summary["entity_count"],
            "relation_count": summary["relation_count"],
            "rejected_relation_count": summary["rejected_relation_count"],
            "draft_path": virtual_session_path(f"intermediate/attempts/{context.attempt_id}/unstructured_draft.json"),
            "concurrency": worker_count,
        },
        ensure_ascii=False,
    )
