"""Persist complete webpage evidence and expose explicit batch fetching."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import tool

from knowcoder_workspace_builder.runtime.credentials import service_api_key
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_invocation_context,
)
from knowcoder_workspace_builder.runtime.retry_policy import (
    call_with_retries,
    is_external_api_error,
)
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.runtime.workspace_sources import register_source_record
from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.tool_calls import FetchLedger
from knowcoder_workspace_builder.storage.transaction import AtomicWriter

from .path_utils import resolve_path
from .web_content import (
    WEB_CONTENT_FORMAT_VERSION,
    FetchedDocument,
    WebFetchSettings,
    canonical_url,
    chunk_markdown,
    crawl_html_documents_sync,
    fetch_document,
    relevant_chunks,
    relevant_excerpt,
    serper_scrape_document,
    source_id_for_url,
)

WEB_FETCH_MAX_RETRIES = 5


def _is_retryable_web_fetch_error(exc: BaseException) -> bool:
    """Retry transport failures and temporary provider responses, but not rejected pages."""
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code in {408, 429} or status_code >= 500
    if isinstance(exc, (ssl.SSLError, TimeoutError, ConnectionError)):
        return True
    return is_external_api_error(exc)


def _positive_number(value: Any, default: float, *, integer: bool = False) -> int | float:
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
        schema_chunk_target_tokens=int(
            _positive_number(configured("schema_chunk_target_tokens", "SCHEMA_CHUNK_TARGET_TOKENS"), 4_096, integer=True)
        ),
        schema_chunk_overlap_tokens=int(
            _positive_number(configured("schema_chunk_overlap_tokens", "SCHEMA_CHUNK_OVERLAP_TOKENS"), 256, integer=True)
        ),
        extraction_chunk_target_tokens=int(
            _positive_number(configured("extraction_chunk_target_tokens", "EXTRACTION_CHUNK_TARGET_TOKENS"), 2_048, integer=True)
        ),
        extraction_chunk_overlap_tokens=int(
            _positive_number(configured("extraction_chunk_overlap_tokens", "EXTRACTION_CHUNK_OVERLAP_TOKENS"), 128, integer=True)
        ),
        relevant_chunks_per_source=int(
            _positive_number(configured("relevant_chunks_per_source", "SCHEMA_WEB_RELEVANT_CHUNKS"), 4, integer=True)
        ),
        relevant_excerpt_chars=int(
            _positive_number(configured("relevant_excerpt_chars", "SCHEMA_WEB_RELEVANT_EXCERPT_CHARS"), 1_600, integer=True)
        ),
        max_concurrency=int(
            _positive_number(configured("max_concurrency", "SCHEMA_WEB_FETCH_CONCURRENCY"), 4, integer=True)
        ),
        user_agent=str(configured("user_agent", "SCHEMA_WEB_FETCH_USER_AGENT") or WebFetchSettings.user_agent),
        browser_channel=str(
            configured("browser_channel", "SCHEMA_WEB_FETCH_BROWSER_CHANNEL") or WebFetchSettings.browser_channel
        ),
        html_provider=str(configured("html_provider", "SCHEMA_WEB_FETCH_PROVIDER") or WebFetchSettings.html_provider),
        serper_scrape_url=str(
            configured("serper_scrape_url", "SCHEMA_SERPER_SCRAPE_URL") or WebFetchSettings.serper_scrape_url
        ),
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


def _persist_candidate(
    document: FetchedDocument,
    settings: WebFetchSettings,
    *,
    step_index: int,
    query: str,
) -> dict[str, Any]:
    """Persist a reviewable page inside the active attempt without registering it as evidence."""
    paths = active_session_paths()
    context = active_invocation_context()
    content_hash = hashlib.sha256(document.markdown.encode("utf-8")).hexdigest()
    source_id = f"{source_id_for_url(document.final_url)}_{content_hash[:12]}"
    candidate_digest = hashlib.sha256(
        f"{context.attempt_id}:{canonical_url(document.final_url)}:{content_hash}".encode()
    ).hexdigest()[:12]
    candidate_id = f"page_{candidate_digest}"
    directory = paths.attempts / context.attempt_id / "web_fetch_candidates" / candidate_id
    chunks = chunk_markdown(candidate_id, document.markdown, settings)
    writer = AtomicWriter(paths)
    raw_path = directory / f"raw{document.raw_suffix}"
    content_path = directory / "content.md"
    chunks_path = directory / "chunks.jsonl"
    metadata_path = directory / "metadata.json"
    if metadata_path.is_file():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, dict)
            or canonical_url(str(existing.get("url") or "")) != canonical_url(document.final_url)
            or str(existing.get("content_sha256") or "") != content_hash
        ):
            raise ValueError(f"Fetched webpage candidate ID conflicts with existing content: {candidate_id}")
    writer.bytes(raw_path, document.raw_bytes)
    writer.text(content_path, document.markdown)
    writer.text(chunks_path, _json_lines(chunks))
    metadata = {
        "format_version": 1,
        "candidate_id": candidate_id,
        "source_id": source_id,
        "attempt_id": context.attempt_id,
        "step_index": step_index,
        "query": query,
        "requested_url": document.requested_url,
        "url": document.final_url,
        "fetch_method": document.fetch_method,
        "title": document.title,
        "content_type": document.content_type,
        "content_sha256": content_hash,
        "character_count": len(document.markdown),
        "schema_chunk_target_tokens": settings.schema_chunk_target_tokens,
        "schema_chunk_overlap_tokens": settings.schema_chunk_overlap_tokens,
        "chunk_count": len(chunks),
        "raw_path": virtual_path_for(paths.root, raw_path),
        "content_path": virtual_path_for(paths.root, content_path),
        "chunk_path": virtual_path_for(paths.root, chunks_path),
    }
    writer.json(metadata_path, metadata)
    return metadata


def _candidate_metadata(candidate_id: str) -> dict[str, Any]:
    context = active_invocation_context()
    path = active_session_paths().attempts / context.attempt_id / "web_fetch_candidates" / candidate_id / "metadata.json"
    if not path.is_file():
        raise ValueError(
            f"Fetched webpage candidate does not exist: {candidate_id}. "
            "Copy candidate_id exactly from a successful fetch_web_pages result."
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("attempt_id") != context.attempt_id:
        raise ValueError(f"Fetched webpage candidate is invalid: {candidate_id}")
    return value


def _candidate_summary(
    metadata: dict[str, Any],
    *,
    query: str,
    settings: WebFetchSettings,
    cached: bool = False,
) -> dict[str, Any]:
    chunks = _read_json_lines(resolve_path(str(metadata["chunk_path"])))
    ranked = relevant_chunks(query, chunks, top_k=settings.relevant_chunks_per_source)
    return {
        "candidate_id": str(metadata["candidate_id"]),
        "requested_url": str(metadata.get("requested_url") or metadata["url"]),
        "final_url": str(metadata["url"]),
        "url": str(metadata["url"]),
        "title": str(metadata["title"]),
        "fetch_method": str(metadata.get("fetch_method") or "unknown"),
        "status": "completed",
        "character_count": int(metadata["character_count"]),
        "chunk_count": int(metadata["chunk_count"]),
        "relevant_chunks": [
            {
                "chunk_id": str(chunk["chunk_id"]),
                "heading": str(chunk.get("heading") or ""),
                "content_excerpt": relevant_excerpt(
                    query,
                    str(chunk.get("text") or ""),
                    max_chars=settings.relevant_excerpt_chars,
                ),
            }
            for chunk in ranked
        ],
        "cached": cached,
    }


def prepare_fetch_candidates(selections: list[dict[str, Any]]) -> tuple[dict[int, dict[str, list[Any]]], list[dict[str, Any]]]:
    """Validate model-selected page chunks and prepare formal records."""
    if not isinstance(selections, list):
        raise ValueError("selected_web_sources must be a list")
    paths = active_session_paths()
    bindings: dict[int, dict[str, list[Any]]] = {}
    records: list[dict[str, Any]] = []
    records_by_source: dict[str, dict[str, Any]] = {}
    seen: set[tuple[int, str, str]] = set()
    for position, selection in enumerate(selections, start=1):
        if not isinstance(selection, dict):
            raise ValueError(f"selected_web_sources item {position} must be an object")
        step_index = selection.get("step_index")
        candidate_id = str(selection.get("candidate_id") or "").strip()
        chunk_ids = selection.get("chunk_ids")
        if not isinstance(step_index, int) or isinstance(step_index, bool):
            raise ValueError(f"selected_web_sources item {position} requires an integer step_index")
        if not candidate_id:
            raise ValueError(f"selected_web_sources item {position} requires candidate_id")
        if not isinstance(chunk_ids, list) or not chunk_ids or any(not str(item).strip() for item in chunk_ids):
            raise ValueError(f"selected_web_sources item {position} requires non-empty chunk_ids")
        normalized_chunk_ids = list(dict.fromkeys(str(item).strip() for item in chunk_ids))
        metadata = _candidate_metadata(candidate_id)
        content_path = resolve_path(str(metadata["content_path"]))
        chunk_path = resolve_path(str(metadata["chunk_path"]))
        raw_path = resolve_path(str(metadata["raw_path"]))
        available_chunks = {str(chunk["chunk_id"]): chunk for chunk in _read_json_lines(chunk_path)}
        missing_chunk_ids = [chunk_id for chunk_id in normalized_chunk_ids if chunk_id not in available_chunks]
        if missing_chunk_ids:
            raise ValueError(
                f"selected_web_sources item {position} references unknown chunks for {candidate_id}: "
                + ", ".join(missing_chunk_ids)
            )
        source_id = str(metadata["source_id"])
        final_directory = paths.sources / "web_crawls" / source_id
        final_content_path = final_directory / "content.md"
        final_chunk_path = final_directory / "chunks.jsonl"
        final_raw_path = final_directory / raw_path.name
        record = records_by_source.get(source_id)
        if record is None:
            record = {
                "source_id": source_id,
                "category": "web_crawls",
                "source_kind": "web_crawl",
                "evidence_group": "web",
                "file_path": virtual_path_for(paths.root, final_content_path),
                "raw_path": virtual_path_for(paths.root, final_raw_path),
                "chunk_path": virtual_path_for(paths.root, final_chunk_path),
                "file_type": "md",
                "url": str(metadata.get("url") or ""),
                "requested_url": str(metadata.get("requested_url") or ""),
                "title": str(metadata.get("title") or ""),
                "content_type": str(metadata.get("content_type") or ""),
                "content_sha256": str(metadata.get("content_sha256") or ""),
                "content_format_version": WEB_CONTENT_FORMAT_VERSION,
                "chunk_count": 0,
                "size_bytes": content_path.stat().st_size,
                "_candidate_content_path": str(content_path),
                "_candidate_chunk_path": str(chunk_path),
                "_candidate_raw_path": str(raw_path),
                "_candidate_id": candidate_id,
                "_candidate_selected_chunk_ids": [],
            }
            records_by_source[source_id] = record
            records.append(record)
        binding = bindings.setdefault(step_index, {"source_ids": [], "chunk_refs": []})
        if source_id not in binding["source_ids"]:
            binding["source_ids"].append(source_id)
        selected_for_source = record["_candidate_selected_chunk_ids"]
        for chunk_id in normalized_chunk_ids:
            selection_key = (step_index, candidate_id, chunk_id)
            if selection_key in seen:
                raise ValueError(
                    f"Fetched webpage chunk was repeated for research step {step_index}: {chunk_id}"
                )
            if chunk_id not in selected_for_source:
                selected_for_source.append(chunk_id)
            binding["chunk_refs"].append(
                {
                    "source_id": source_id,
                    "chunk_id": chunk_id.replace(candidate_id, source_id, 1),
                }
            )
            seen.add(selection_key)
    for record in records:
        record["chunk_count"] = len(record["_candidate_selected_chunk_ids"])
    return bindings, records


def register_prepared_fetch_sources(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paths = active_session_paths()
    registered: list[dict[str, Any]] = []
    writer = AtomicWriter(paths)
    for prepared in records:
        record = {key: value for key, value in prepared.items() if not key.startswith("_candidate_")}
        source_id = str(record["source_id"])
        candidate_id = str(prepared["_candidate_id"])
        content_path = Path(str(prepared["_candidate_content_path"]))
        chunk_path = Path(str(prepared["_candidate_chunk_path"]))
        raw_path = Path(str(prepared["_candidate_raw_path"]))
        selected_chunk_ids = set(prepared["_candidate_selected_chunk_ids"])
        chunks = [
            chunk
            for chunk in _read_json_lines(chunk_path)
            if str(chunk.get("chunk_id") or "") in selected_chunk_ids
        ]
        normalized_chunks = [
            {
                **chunk,
                "source_id": source_id,
                "chunk_id": str(chunk["chunk_id"]).replace(candidate_id, source_id, 1),
            }
            for chunk in chunks
        ]
        writer.text(resolve_path(str(record["file_path"])), content_path.read_text(encoding="utf-8"))
        writer.text(resolve_path(str(record["chunk_path"])), _json_lines(normalized_chunks))
        writer.bytes(resolve_path(str(record["raw_path"])), raw_path.read_bytes())
        registered.append(register_source_record(paths.root, "web_crawls", record))
    return registered


def fetch_candidate_pages(
    urls: list[str],
    *,
    step_index: int,
    query: str,
    settings: WebFetchSettings | None = None,
) -> dict[str, Any]:
    """Fetch review candidates without registering them as formal Workspace sources."""
    active_settings = settings or load_web_fetch_settings()
    normalized_urls = list(dict.fromkeys(canonical_url(value) for value in urls))
    if not normalized_urls:
        raise ValueError("At least one webpage URL is required")
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    lightweight_urls = [url for url in normalized_urls if urlsplit(url).path.casefold().endswith((".pdf", ".txt"))]
    html_urls = [url for url in normalized_urls if url not in lightweight_urls]
    documents: dict[str, FetchedDocument] = {}
    provider = active_settings.html_provider.strip().casefold()
    if provider not in {"serper", "crawl4ai"}:
        raise ValueError("Web fetch html_provider must be 'serper' or 'crawl4ai'")
    if html_urls and provider == "crawl4ai":
        crawled, crawl_failures = crawl_html_documents_sync(html_urls, active_settings)
        documents.update(crawled)
        failures.extend(
            {
                **failure,
                "requested_url": str(failure.get("url") or ""),
                "fetch_method": "crawl4ai",
                "status": "failed",
            }
            for failure in crawl_failures
        )
    if html_urls and provider == "serper":
        api_key = service_api_key("SERPER_API_KEY")
        if not api_key:
            raise ValueError("SERPER_API_KEY is required for the Serper web fetch provider")
        with ThreadPoolExecutor(max_workers=min(active_settings.max_concurrency, len(html_urls))) as pool:
            futures = {
                pool.submit(
                    call_with_retries,
                    lambda candidate_url=url: serper_scrape_document(
                        candidate_url,
                        active_settings,
                        api_key=api_key,
                    ),
                    is_retryable=_is_retryable_web_fetch_error,
                    max_retries=WEB_FETCH_MAX_RETRIES,
                ): url
                for url in html_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    documents[url] = future.result()
                except Exception as exc:  # noqa: BLE001 - every failed candidate is reported explicitly.
                    failures.append(
                        {
                            "url": url,
                            "requested_url": url,
                            "fetch_method": "serper_scrape",
                            "status": "failed",
                            "error": str(exc),
                        }
                    )
    with ThreadPoolExecutor(max_workers=min(active_settings.max_concurrency, len(lightweight_urls) or 1)) as pool:
        futures = {
            pool.submit(
                call_with_retries,
                lambda candidate_url=url: fetch_document(candidate_url, active_settings),
                is_retryable=_is_retryable_web_fetch_error,
                max_retries=WEB_FETCH_MAX_RETRIES,
            ): url
            for url in lightweight_urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                documents[url] = future.result()
            except Exception as exc:  # noqa: BLE001 - every failed candidate is reported explicitly.
                failures.append(
                    {
                        "url": url,
                        "requested_url": url,
                        "fetch_method": "http_pdf" if urlsplit(url).path.casefold().endswith(".pdf") else "http_text",
                        "status": "failed",
                        "error": str(exc),
                    }
                )
    for url in normalized_urls:
        document = documents.get(url)
        if document is None:
            continue
        metadata = _persist_candidate(document, active_settings, step_index=step_index, query=query)
        candidates.append(_candidate_summary(metadata, query=query, settings=active_settings))
    return {
        "ok": bool(candidates),
        "status": "completed" if len(candidates) == len(normalized_urls) else "partial" if candidates else "failed",
        "candidates": candidates,
        "failures": failures,
        "requested": len(normalized_urls),
        "completed": len(candidates),
        "instruction": (
            "Judge the returned relevant chunks against the complete question and current step. "
            "Save each adopted page with its candidate_id and selected chunk_ids."
        ),
    }


def _prior_url_outcomes(ledger: FetchLedger) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for record in ledger.records():
        response = record.get("response")
        if not isinstance(response, dict):
            continue
        for failure in response.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            url = str(failure.get("url") or "").strip()
            error = str(failure.get("error") or "").strip()
            if url and error:
                outcomes[canonical_url(url)] = {
                    "status": "failed",
                    "error": error,
                    "fetch_method": str(failure.get("fetch_method") or "unknown"),
                }
        for candidate in response.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            url = str(candidate.get("requested_url") or candidate.get("url") or "").strip()
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if url and candidate_id:
                outcomes[canonical_url(url)] = {"status": "completed", "candidate_id": candidate_id}
    return outcomes


def _fetch_with_attempt_cache(
    urls: list[str],
    *,
    step_index: int,
    query: str,
    settings: WebFetchSettings,
    ledger: FetchLedger,
) -> dict[str, Any]:
    normalized_urls = list(dict.fromkeys(canonical_url(value) for value in urls))
    outcomes = _prior_url_outcomes(ledger)
    pending_urls: list[str] = []
    candidates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for url in normalized_urls:
        outcome = outcomes.get(url)
        if outcome is None:
            pending_urls.append(url)
        elif outcome["status"] == "completed":
            metadata = _candidate_metadata(str(outcome["candidate_id"]))
            candidates.append(_candidate_summary(metadata, query=query, settings=settings, cached=True))
        else:
            failures.append(
                {
                    "url": url,
                    "requested_url": url,
                    "fetch_method": str(outcome.get("fetch_method") or "unknown"),
                    "status": "failed",
                    "error": str(outcome["error"]),
                    "cached": True,
                }
            )
    if pending_urls:
        fetched = fetch_candidate_pages(
            pending_urls,
            step_index=step_index,
            query=query,
            settings=settings,
        )
        candidates.extend(list(fetched.get("candidates") or []))
        failures.extend(list(fetched.get("failures") or []))
    completed = len(candidates)
    return {
        "ok": completed > 0,
        "status": "completed" if completed == len(normalized_urls) else "partial" if completed else "failed",
        "candidates": candidates,
        "failures": failures,
        "requested": len(normalized_urls),
        "completed": completed,
        "instruction": (
            "Judge the returned relevant chunks against the complete question and current step. "
            "Save each adopted page with its candidate_id and selected chunk_ids."
        ),
    }


@tool
def fetch_web_pages(urls: list[str], step_index: int, purpose: str) -> str:
    """Fetch URL candidates in parallel for one evidence goal and return relevant chunks per URL."""
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
        paths = active_session_paths()
        settings = load_web_fetch_settings()
        ledger = FetchLedger(paths, context.attempt_id)
        locks = SessionLockStore(ProjectLayout(paths.project))
        with locks.acquire_scope(paths.session_id, "web_fetch"):
            result = _fetch_with_attempt_cache(
                urls,
                step_index=step_index,
                query=query,
                settings=settings,
                ledger=ledger,
            )
            ledger.append(
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
