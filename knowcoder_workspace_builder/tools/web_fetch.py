"""Persist complete webpage evidence and expose explicit batch fetching."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.live_events import emit_worker_event
from knowcoder_workspace_builder.runtime.retry_policy import call_with_retries, is_external_api_error
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.runtime.workspace_sources import register_source_record, register_source_version, source_records
from knowcoder_workspace_builder.storage.tool_calls import FetchLedger
from knowcoder_workspace_builder.storage.transaction import AtomicWriter

from .path_utils import resolve_path
from .web_content import (
    FetchedDocument,
    WEB_CONTENT_FORMAT_VERSION,
    WebFetchSettings,
    canonical_url,
    chunk_markdown,
    fetch_document,
    relevant_chunks,
    relevant_excerpt,
    source_id_for_url,
)


WEB_FETCH_MAX_RETRIES = 1


def _is_retryable_web_fetch_error(exc: BaseException) -> bool:
    """Retry transient transport failures, never explicit client rejection."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in {408, 429} or status_code >= 500
    if isinstance(exc, (ssl.SSLError, TimeoutError, ConnectionError)):
        return True
    return is_external_api_error(exc)


def _positive_number(value: Any, default: int | float, *, integer: bool = False) -> int | float:
    if value in {None, ""}:
        return default
    parsed = int(value) if integer else float(value)
    if parsed <= 0:
        raise ValueError("Web fetch settings must contain positive values")
    return parsed


def _raw_services_config() -> dict[str, Any]:
    roots = [
        Path(value).expanduser()
        for value in (
            os.environ.get("HARNESS_ROOT", ""),
            os.environ.get("KNOWCODER_BUILDER_ROOT", ""),
            str(Path(__file__).resolve().parents[1]),
            os.getcwd(),
        )
        if value
    ]
    config_path = next((root / "harness.json" for root in roots if (root / "harness.json").is_file()), None)
    if config_path is None:
        return {}
    value = json.loads(config_path.read_text(encoding="utf-8"))
    services = value.get("services") if isinstance(value, dict) else None
    web_fetch = services.get("web_fetch") if isinstance(services, dict) else None
    return dict(web_fetch) if isinstance(web_fetch, dict) else {}


def load_web_fetch_settings() -> WebFetchSettings:
    config = _raw_services_config()

    def configured(name: str, env_name: str) -> Any:
        return os.environ.get(env_name) or config.get(name)

    return WebFetchSettings(
        timeout_seconds=float(_positive_number(configured("timeout_seconds", "SCHEMA_WEB_FETCH_TIMEOUT"), 20.0)),
        max_response_bytes=int(
            _positive_number(configured("max_response_bytes", "SCHEMA_WEB_FETCH_MAX_BYTES"), 8_000_000, integer=True)
        ),
        min_content_chars=int(
            _positive_number(configured("min_content_chars", "SCHEMA_WEB_FETCH_MIN_CONTENT_CHARS"), 160, integer=True)
        ),
        chunk_target_chars=int(
            _positive_number(configured("chunk_target_chars", "SCHEMA_WEB_CHUNK_TARGET_CHARS"), 4_000, integer=True)
        ),
        chunk_overlap_chars=int(
            _positive_number(configured("chunk_overlap_chars", "SCHEMA_WEB_CHUNK_OVERLAP_CHARS"), 240, integer=True)
        ),
        relevant_chunks_per_source=int(
            _positive_number(configured("relevant_chunks_per_source", "SCHEMA_WEB_RELEVANT_CHUNKS"), 4, integer=True)
        ),
        relevant_excerpt_chars=int(
            _positive_number(configured("relevant_excerpt_chars", "SCHEMA_WEB_RELEVANT_EXCERPT_CHARS"), 1_600, integer=True)
        ),
        successful_pages_per_search=int(
            _positive_number(configured("successful_pages_per_search", "SCHEMA_WEB_PAGES_PER_SEARCH"), 2, integer=True)
        ),
        max_concurrency=int(
            _positive_number(configured("max_concurrency", "SCHEMA_WEB_FETCH_CONCURRENCY"), 4, integer=True)
        ),
        user_agent=str(configured("user_agent", "SCHEMA_WEB_FETCH_USER_AGENT") or WebFetchSettings.user_agent),
    )


def _json_lines(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Chunk record at line {line_number} must be an object")
        records.append(value)
    return records


def load_source_chunks(record: dict[str, Any]) -> list[dict[str, Any]]:
    chunk_path = str(record.get("chunk_path") or "").strip()
    if not chunk_path:
        return []
    return _read_json_lines(resolve_path(chunk_path))


def _cached_record(url: str) -> dict[str, Any] | None:
    requested = canonical_url(url)
    attempt_id = active_invocation_context().attempt_id
    for record in source_records(active_session_paths().root):
        if str(record.get("source_kind") or "") != "web_crawl":
            continue
        if int(record.get("content_format_version") or 0) != WEB_CONTENT_FORMAT_VERSION:
            continue
        if record.get("status") == "superseded" or str(record.get("attempt_id") or "") != attempt_id:
            continue
        values = {str(record.get(field) or "").strip() for field in ("url", "requested_url")}
        try:
            canonical_values = {canonical_url(value) for value in values if value}
        except ValueError:
            canonical_values = set()
        if requested not in canonical_values:
            continue
        file_path = str(record.get("file_path") or "")
        chunk_path = str(record.get("chunk_path") or "")
        if file_path and chunk_path and resolve_path(file_path).is_file() and resolve_path(chunk_path).is_file():
            return dict(record)
    return None


def _record_url(record: dict[str, Any]) -> str:
    value = str(record.get("url") or record.get("requested_url") or "").strip()
    if not value:
        return ""
    try:
        return canonical_url(value)
    except ValueError:
        return ""


def _persist_document(document: FetchedDocument, settings: WebFetchSettings) -> dict[str, Any]:
    paths = active_session_paths()
    content_hash = hashlib.sha256(document.markdown.encode("utf-8")).hexdigest()
    base_source_id = source_id_for_url(document.final_url)
    current_records = source_records(paths.root)
    same_url = [
        dict(item)
        for item in current_records
        if str(item.get("source_kind") or "") == "web_crawl"
        and _record_url(item) == canonical_url(document.final_url)
    ]
    existing = next(
        (item for item in same_url if str(item.get("content_sha256") or "") == content_hash),
        None,
    )
    if existing is not None:
        return existing
    source_id = f"{base_source_id}_{content_hash[:12]}"
    directory = paths.sources / "web_crawls" / source_id
    raw_path = directory / f"raw{document.raw_suffix}"
    content_path = directory / "content.md"
    metadata_path = directory / "metadata.json"
    chunks_path = directory / "chunks.jsonl"
    chunks = chunk_markdown(source_id, document.markdown, settings)
    retrieved_at = datetime.now(UTC).isoformat()
    writer = AtomicWriter(paths)
    writer.bytes(raw_path, document.raw_bytes)
    writer.text(content_path, document.markdown)
    writer.text(chunks_path, _json_lines(chunks))
    metadata = {
        "format_version": 2,
        "content_format_version": WEB_CONTENT_FORMAT_VERSION,
        "source_id": source_id,
        "source_family_id": base_source_id,
        "requested_url": document.requested_url,
        "url": document.final_url,
        "title": document.title,
        "content_type": document.content_type,
        "retrieved_at": retrieved_at,
        "content_sha256": content_hash,
        "raw_sha256": hashlib.sha256(document.raw_bytes).hexdigest(),
        "character_count": len(document.markdown),
        "chunk_count": len(chunks),
        "attempt_id": active_invocation_context().attempt_id,
    }
    writer.json(metadata_path, metadata)
    record = {
            **metadata,
            "source_kind": "web_crawl",
            "evidence_group": "web",
            "file_path": virtual_path_for(paths.root, content_path),
            "raw_path": virtual_path_for(paths.root, raw_path),
            "metadata_path": virtual_path_for(paths.root, metadata_path),
            "chunk_path": virtual_path_for(paths.root, chunks_path),
            "file_type": "md",
            "size_bytes": content_path.stat().st_size,
            "fetch_provider": "direct",
        }
    active_previous = [
        str(item.get("source_id") or "")
        for item in same_url
        if item.get("status") != "superseded" and str(item.get("source_id") or "")
    ]
    if active_previous:
        return register_source_version(paths.root, "web_crawls", record, supersedes=active_previous)
    return register_source_record(paths.root, "web_crawls", {**record, "status": "active"})


def _emit_fetch_progress(status: str, content: str, call_id: str) -> None:
    emit_worker_event(
        {
            "type": "activity",
            "stage": "evidence",
            "run_agent": "workspace_builder",
            "message": {
                "role": "event",
                "kind": "tool",
                "content": content,
                "tool": "web_content_fetch",
                "tool_call_id": call_id,
                "status": status,
                "stage": "evidence",
                "run_agent": "workspace_builder",
            },
        }
    )


def _public_source(record: dict[str, Any], query: str, settings: WebFetchSettings, *, cached: bool) -> dict[str, Any]:
    chunks = load_source_chunks(record)
    selected = relevant_chunks(query, chunks, top_k=settings.relevant_chunks_per_source)
    return {
        "source_id": str(record.get("source_id") or ""),
        "url": str(record.get("url") or ""),
        "title": str(record.get("title") or ""),
        "content_sha256": str(record.get("content_sha256") or ""),
        "chunk_count": int(record.get("chunk_count") or len(chunks)),
        "cached": cached,
        "relevant_chunks": [
            {
                "source_id": str(item.get("source_id") or ""),
                "chunk_id": str(item.get("chunk_id") or ""),
                "heading": str(item.get("heading") or ""),
                "text": relevant_excerpt(
                    query,
                    str(item.get("text") or ""),
                    max_chars=settings.relevant_excerpt_chars,
                ),
                "score": float(item.get("score") or 0),
            }
            for item in selected
        ],
    }


def fetch_and_store_pages(
    urls: list[str],
    *,
    query: str,
    target_successes: int | None = None,
    settings: WebFetchSettings | None = None,
) -> dict[str, Any]:
    active_settings = settings or load_web_fetch_settings()
    normalized_urls: list[str] = []
    for value in urls:
        url = canonical_url(value)
        if url not in normalized_urls:
            normalized_urls.append(url)
    if not normalized_urls:
        raise ValueError("At least one webpage URL is required")
    target = len(normalized_urls) if target_successes is None else min(len(normalized_urls), max(1, target_successes))
    sources: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    pending: list[str] = []
    for url in normalized_urls:
        cached = _cached_record(url)
        if cached is None:
            pending.append(url)
        else:
            sources.append(_public_source(cached, query, active_settings, cached=True))
        if len(sources) >= target:
            break
    cursor = 0
    while len(sources) < target and cursor < len(pending):
        needed = target - len(sources)
        wave = pending[cursor : cursor + min(active_settings.max_concurrency, needed)]
        cursor += len(wave)
        call_id = f"web-content:{cursor}"
        _emit_fetch_progress("running", f"Fetching {len(wave)} webpage source(s).", call_id)
        fetched: dict[str, FetchedDocument] = {}
        with ThreadPoolExecutor(max_workers=min(active_settings.max_concurrency, len(wave))) as pool:
            futures = {
                pool.submit(
                    call_with_retries,
                    lambda target=url: fetch_document(target, active_settings),
                    is_retryable=_is_retryable_web_fetch_error,
                    max_retries=WEB_FETCH_MAX_RETRIES,
                ): url
                for url in wave
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    fetched[url] = future.result()
                except Exception as exc:  # noqa: BLE001 - recorded as an explicit source failure.
                    failures.append({"url": url, "error": str(exc)})
        _emit_fetch_progress("done", f"Fetched {len(fetched)} complete webpage response(s).", call_id)
        index_call_id = f"{call_id}:index"
        if fetched:
            _emit_fetch_progress("running", f"Indexing {len(fetched)} complete webpage source(s).", index_call_id)
        for url in wave:
            document = fetched.get(url)
            if document is None:
                continue
            try:
                record = _persist_document(document, active_settings)
                sources.append(_public_source(record, query, active_settings, cached=False))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                failures.append({"url": url, "error": str(exc)})
        if fetched:
            _emit_fetch_progress("done", f"Indexed {len(sources)} complete webpage source(s).", index_call_id)
    source_ids = [str(item["source_id"]) for item in sources]
    chunk_refs = [
        {"source_id": str(chunk["source_id"]), "chunk_id": str(chunk["chunk_id"])}
        for source in sources
        for chunk in source.get("relevant_chunks") or []
    ]
    return {
        "ok": bool(sources),
        "status": "completed" if len(sources) >= target else "partial" if sources else "failed",
        "sources": sources,
        "failures": failures,
        "coverage_binding": {"source_ids": source_ids, "chunk_refs": chunk_refs},
        "requested": len(normalized_urls),
        "target_successes": target,
        "completed": len(sources),
    }


@tool
def fetch_web_pages(urls: list[str], step_index: int, purpose: str) -> str:
    """Fetch complete content for explicit webpage URLs and bind it to one research step."""
    try:
        context = active_invocation_context()
        if context.stage != "evidence":
            raise ValueError("fetch_web_pages is available only during evidence collection")
        steps = list(context.input.get("steps") or [])
        if not isinstance(step_index, int) or isinstance(step_index, bool) or not 1 <= step_index <= len(steps):
            raise ValueError(f"step_index must be an integer from 1 through {len(steps)}")
        if not isinstance(urls, list) or not urls:
            raise ValueError("urls must be a non-empty list")
        normalized_purpose = str(purpose or "").strip()
        if not normalized_purpose:
            raise ValueError("purpose must be non-empty text")
        query = f"{steps[step_index - 1]} {normalized_purpose}"
        result = fetch_and_store_pages(urls, query=query)
        binding = dict(result.get("coverage_binding") or {})
        binding["step_index"] = step_index
        result["coverage_binding"] = binding
        FetchLedger(active_session_paths(), context.attempt_id).append(
            {
                "step_index": step_index,
                "purpose": normalized_purpose,
                "urls": [canonical_url(value) for value in urls],
                "status": str(result.get("status") or "failed"),
                "response": result,
            }
        )
        if not result.get("ok"):
            result["error_type"] = "web_fetch_error"
            result["message"] = "No requested webpage produced usable complete content."
        return json.dumps(result, ensure_ascii=False)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return json.dumps({"ok": False, "error_type": "web_fetch_error", "error": str(exc)}, ensure_ascii=False)
