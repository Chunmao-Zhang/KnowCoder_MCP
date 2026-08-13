"""Batch unstructured extraction backed by one model request per assigned chunk."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_core.tools import tool
from openai import OpenAI

from knowcoder_workspace_builder.contracts.errors import ContractError, ExternalServiceError
from knowcoder_workspace_builder.runtime.candidate_normalization import normalize_instance_batch
from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.live_events import emit_worker_event
from knowcoder_workspace_builder.runtime.retry_policy import is_external_api_error, wait_before_retry
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
FALLBACK_WORKERS_ENV = "SCHEMA_EXTRACT_FALLBACK_WORKERS"
RETRY_LIMIT_ENV = "SCHEMA_EXTRACT_CHUNK_RETRY_LIMIT"
FORMAT_REPAIR_LIMIT_ENV = "SCHEMA_EXTRACT_FORMAT_REPAIR_LIMIT"

DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_TOKENS = 4_096
DEFAULT_WORKERS = 10
DEFAULT_FALLBACK_WORKERS = 2
DEFAULT_RETRY_LIMIT = 5
DEFAULT_FORMAT_REPAIR_LIMIT = 2
MAX_WORKERS = 32

ExtractionProgressSink = Callable[[int, int, str, int], None]

EXTRACTION_SYSTEM_PROMPT = """# Role
You are an unstructured information extractor.

# Objective
Extract every entity and relation in one source chunk that fits the supplied Schema outline.

# Constraints
- Treat the source chunk as the complete factual input.
- Use only facts explicitly stated in the source chunk.
- Preserve numbers, units, dates, ranges, scope, and qualifications.
- Use the Schema outline to determine the target entity types, attributes, and relations.
- Create stable, readable IDs within each entity type.
- Include every relation endpoint in entities with the same type and ID.
- Verify every relation endpoint resolves to an entity in the response.
- Represent attributes as JSON objects. Use an empty object when the chunk has no attribute facts.
- Put factual details in attributes using JSON-compatible values.
- Return empty arrays when the chunk contains no relevant facts.
- Return strict JSON with only entities and relations.

# Output Contract
Each entity contains type, id, name, and attributes.
Each relation contains type, head, tail, and attributes.
Relation head and tail are objects containing type and id.
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
    return OpenAI(api_key=api_key, base_url=base_url, timeout=_timeout_setting()), model


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
    evidence_refs = [{"source_id": chunk["source_id"], "chunk_id": chunk["chunk_id"]}] if chunk["chunk_id"] else []
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    repair_limit = _integer_setting(
        FORMAT_REPAIR_LIMIT_ENV,
        DEFAULT_FORMAT_REPAIR_LIMIT,
        minimum=0,
        maximum=5,
    )
    for repair_count in range(repair_limit + 1):
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=_integer_setting(MAX_TOKENS_ENV, DEFAULT_MAX_TOKENS, minimum=1, maximum=16_384),
            response_format={"type": "json_object"},
            messages=messages,
        )
        if not response.choices:
            raise ExternalServiceError("Extraction model returned no choices", model=model)
        content = response.choices[0].message.content or ""
        try:
            batch, parse_changes = _json_object(content)
            entities, relations, changes = normalize_instance_batch(
                entities=batch["entities"],
                relations=batch["relations"],
                source_ids=[chunk["source_id"]],
                evidence_refs=evidence_refs,
            )
            changes = [*parse_changes, *changes]
        except ContractError as exc:
            if repair_count >= repair_limit:
                raise
            messages.extend(
                [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "Correct the JSON object so it satisfies the Output Contract. "
                            f"Validation error: {exc}. Every entity and relation must include an attributes "
                            "JSON object; use an empty object when there are no attribute facts. "
                            "Return only the corrected entities and relations object."
                        ),
                    },
                ]
            )
            continue
        return {
            "entities": entities,
            "relations": relations,
            "format_repair_count": repair_count,
            "normalization_changes": [
                change
                for change in changes
                if change.get("action") in {"derived", "ignored", "moved", "wrapped"}
            ],
        }
    raise AssertionError("Extraction format repair loop exhausted without returning")


def _is_transient_api_error(exc: BaseException) -> bool:
    return is_external_api_error(exc)


def _is_rate_limit_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "RateLimitError"


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
) -> tuple[dict[int, dict[str, Any]], dict[int, int], int, bool]:
    max_workers = min(
        len(chunks),
        _integer_setting(WORKERS_ENV, DEFAULT_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    fallback_workers = min(
        max_workers,
        _integer_setting(FALLBACK_WORKERS_ENV, DEFAULT_FALLBACK_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    retry_limit = _integer_setting(RETRY_LIMIT_ENV, DEFAULT_RETRY_LIMIT, minimum=0, maximum=10)
    pending = list(enumerate(chunks))
    results: dict[int, dict[str, Any]] = {}
    retries: dict[int, int] = {}
    active_workers = max_workers
    fallback_triggered = False

    while pending:
        wave, pending = pending[:active_workers], pending[active_workers:]
        if on_progress is not None:
            for index, _ in wave:
                on_progress(index + 1, len(chunks), "running", len(results))
        failures: list[tuple[int, dict[str, Any], BaseException]] = []
        with ThreadPoolExecutor(max_workers=min(active_workers, len(wave))) as pool:
            futures = {
                pool.submit(_extract_one, client, model, schema_outline, chunk): (index, chunk) for index, chunk in wave
            }
            for future in as_completed(futures):
                index, chunk = futures[future]
                try:
                    results[index] = future.result()
                except BaseException as exc:  # Preserve the concrete provider error for retry classification.
                    failures.append((index, chunk, exc))
                    continue
                if on_progress is not None:
                    # Publish as soon as this request finishes. Waiting for the
                    # whole worker wave makes a ten-worker run appear to jump
                    # from 0 to 10 even though chunks completed one by one.
                    on_progress(index + 1, len(chunks), "done", len(results))
        if not failures:
            continue
        should_reduce_concurrency = any(
            _is_transient_api_error(error) and not _is_rate_limit_error(error) for _, _, error in failures
        )
        if should_reduce_concurrency and active_workers > fallback_workers:
            active_workers = fallback_workers
            fallback_triggered = True
        retry_items: list[tuple[int, dict[str, Any]]] = []
        for index, chunk, error in failures:
            count = retries.get(index, 0)
            if count < retry_limit:
                retries[index] = count + 1
                retry_items.append((index, chunk))
                continue
            if _is_transient_api_error(error):
                if on_progress is not None:
                    on_progress(index + 1, len(chunks), "failed", len(results))
                raise ExternalServiceError(
                    "Unstructured extraction API failed after retries",
                    model=model,
                    error_type=type(error).__name__,
                    chunk_index=index + 1,
                ) from error
            if on_progress is not None:
                on_progress(index + 1, len(chunks), "failed", len(results))
            raise error
        pending = retry_items + pending
        if retry_items:
            wait_before_retry(max(retries[index] for index, _chunk in retry_items))
    return results, retries, active_workers, fallback_triggered


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
    initial_workers = min(
        len(chunks),
        _integer_setting(WORKERS_ENV, DEFAULT_WORKERS, minimum=1, maximum=MAX_WORKERS),
    )
    results, retries, final_workers, fallback_triggered = _run_chunk_requests(
        client,
        model,
        schema_outline,
        chunks,
        on_progress=_emit_extraction_progress,
    )

    draft = empty_draft()
    for index in range(len(chunks)):
        draft = merge_draft(draft, results[index])
    normalization_entries = [
        {
            "unit_index": index + 1,
            "source_id": chunks[index]["source_id"],
            "chunk_id": chunks[index]["chunk_id"],
            "changes": list(results[index].get("normalization_changes") or []),
        }
        for index in range(len(chunks))
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
        "initial_concurrency": initial_workers,
        "final_concurrency": final_workers,
        "fallback_triggered": fallback_triggered,
        "chunk_retry_counts": {str(index + 1): count for index, count in sorted(retries.items())},
        "format_repair_counts": {
            str(index + 1): int(result.get("format_repair_count") or 0)
            for index, result in sorted(results.items())
            if result.get("format_repair_count")
        },
        "entity_count": len(validated["entities"]),
        "relation_count": len(validated["relations"]),
        "rejected_relation_count": len(rejected_relations),
        "normalization_change_count": sum(len(item["changes"]) for item in normalization_entries),
    }
    AtomicWriter(paths).json(paths.attempts / context.attempt_id / "extract_unit_results.json", summary)
    return json.dumps(
        {
            "ok": True,
            "processed_chunks": len(chunks),
            "entity_count": summary["entity_count"],
            "relation_count": summary["relation_count"],
            "rejected_relation_count": summary["rejected_relation_count"],
            "draft_path": virtual_session_path(f"intermediate/attempts/{context.attempt_id}/unstructured_draft.json"),
            "initial_concurrency": initial_workers,
            "final_concurrency": final_workers,
            "fallback_triggered": fallback_triggered,
        },
        ensure_ascii=False,
    )
