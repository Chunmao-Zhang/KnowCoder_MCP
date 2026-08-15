"""Framework-independent webpage fetching, extraction, chunking, and ranking."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import re
import socket
import time
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pypdf import PdfReader

from knowcoder_workspace_builder.runtime.token_chunks import token_chunks

WEB_CONTENT_FORMAT_VERSION = 3
_RETRIEVAL_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "document",
        "documentation",
        "for",
        "from",
        "in",
        "of",
        "official",
        "on",
        "page",
        "source",
        "sources",
        "the",
        "to",
        "website",
        "with",
    }
)


@dataclass(frozen=True)
class WebFetchSettings:
    timeout_seconds: float = 20.0
    max_response_bytes: int = 8_000_000
    min_content_chars: int = 160
    schema_chunk_target_tokens: int = 4_096
    schema_chunk_overlap_tokens: int = 256
    extraction_chunk_target_tokens: int = 2_048
    extraction_chunk_overlap_tokens: int = 128
    relevant_chunks_per_source: int = 4
    relevant_excerpt_chars: int = 1_600
    max_concurrency: int = 4
    user_agent: str = "SchemaWorkspaceBuilder/0.1 (+research evidence fetcher)"
    browser_channel: str = "chromium"
    html_provider: str = "crawl4ai"
    serper_scrape_url: str = "https://scrape.serper.dev"


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    title: str
    content_type: str
    raw_bytes: bytes
    raw_suffix: str
    markdown: str
    fetch_method: str = "unknown"


def canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Webpage URL must use http or https and include a host")
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    port = parsed.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def source_id_for_url(value: str) -> str:
    identity = f"v{WEB_CONTENT_FORMAT_VERSION}:{canonical_url(value)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"crawl_{digest[:20]}"


def _require_public_host(value: str) -> None:
    parsed = urlsplit(canonical_url(value))
    host = str(parsed.hostname or "")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError(f"Webpage host could not be resolved: {host}") from exc
    if not addresses:
        raise ValueError(f"Webpage host did not resolve to an address: {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"Webpage URL resolves to a non-public address: {host}")


def _result_value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _crawl_markdown(result: Any) -> str:
    value = _result_value(result, "markdown", "")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("raw_markdown") or value.get("fit_markdown") or "")
    return str(getattr(value, "raw_markdown", "") or getattr(value, "fit_markdown", ""))


def _crawl_document(requested_url: str, result: Any, settings: WebFetchSettings) -> FetchedDocument:
    if not bool(_result_value(result, "success", False)):
        message = str(_result_value(result, "error_message", "") or "Crawl4AI did not return usable content")
        raise ValueError(f"Crawl4AI failed for {requested_url}: {message}")
    final_url = canonical_url(
        str(_result_value(result, "redirected_url", "") or _result_value(result, "url", "") or requested_url)
    )
    _require_public_host(final_url)
    raw_html = str(_result_value(result, "html", "") or _result_value(result, "cleaned_html", ""))
    raw_bytes = raw_html.encode("utf-8")
    if len(raw_bytes) > settings.max_response_bytes:
        raise ValueError(f"Webpage response exceeds the configured {settings.max_response_bytes} byte limit")
    markdown = _crawl_markdown(result).strip()
    metadata = _result_value(result, "metadata", {}) or {}
    title_value = metadata.get("title", "") if isinstance(metadata, dict) else getattr(metadata, "title", "")
    fallback_title = urlsplit(final_url).path.rsplit("/", 1)[-1] or urlsplit(final_url).hostname or "Web source"
    title = " ".join(str(title_value or fallback_title).split())
    if markdown and not markdown.lstrip().startswith("#"):
        markdown = f"# {title}\n\n{markdown}"
    visible_chars = len(re.sub(r"\s+", "", markdown))
    if visible_chars < settings.min_content_chars:
        raise ValueError(f"Extracted webpage content is too short ({visible_chars} characters) to use as evidence")
    return FetchedDocument(
        requested_url=requested_url,
        final_url=final_url,
        title=title,
        content_type="text/html",
        raw_bytes=raw_bytes,
        raw_suffix=".html",
        markdown=markdown + "\n",
        fetch_method="crawl4ai",
    )


def _index_crawl_results(
    requested_urls: list[str],
    results: list[Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Associate unordered Crawl4AI batch results with their requested URLs."""
    requested = set(requested_urls)
    indexed: dict[str, Any] = {}
    failures: list[dict[str, str]] = []
    for result in results:
        raw_result_url = str(_result_value(result, "url", "") or "").strip()
        if not raw_result_url:
            failures.append(
                {
                    "url": "<unknown>",
                    "error": "Crawl4AI returned a result without its requested URL",
                }
            )
            continue
        try:
            result_url = canonical_url(raw_result_url)
        except (TypeError, ValueError) as exc:
            failures.append({"url": raw_result_url, "error": f"Crawl4AI returned an invalid result URL: {exc}"})
            continue
        if result_url not in requested:
            failures.append(
                {
                    "url": result_url,
                    "error": "Crawl4AI returned a result for a URL outside the requested batch",
                }
            )
            continue
        if result_url in indexed:
            failures.append(
                {
                    "url": result_url,
                    "error": "Crawl4AI returned more than one result for this URL",
                }
            )
            continue
        indexed[result_url] = result
    return indexed, failures


async def crawl_html_documents(
    urls: list[str],
    settings: WebFetchSettings,
    *,
    validate_network: bool = True,
) -> tuple[dict[str, FetchedDocument], list[dict[str, str]]]:
    """Render HTML URLs in one Crawl4AI browser and report failures per URL."""
    from crawl4ai import (
        AsyncWebCrawler,
        BrowserConfig,
        CacheMode,
        CrawlerRunConfig,
        MemoryAdaptiveDispatcher,
    )

    requested_urls = [canonical_url(url) for url in urls]
    if validate_network:
        for url in requested_urls:
            _require_public_host(url)
    browser_config = BrowserConfig(headless=True, chrome_channel=settings.browser_channel, user_agent=settings.user_agent)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=int(settings.timeout_seconds * 1000),
    )
    documents: dict[str, FetchedDocument] = {}
    failures: list[dict[str, str]] = []
    async with AsyncWebCrawler(config=browser_config) as crawler:
        results = await crawler.arun_many(
            requested_urls,
            config=run_config,
            dispatcher=MemoryAdaptiveDispatcher(
                memory_threshold_percent=90,
                check_interval=1,
                max_session_permit=min(settings.max_concurrency, len(requested_urls)),
            ),
        )
    results_by_url, association_failures = _index_crawl_results(requested_urls, list(results))
    failures.extend(association_failures)
    for requested_url in requested_urls:
        result = results_by_url.get(requested_url)
        if result is None:
            failures.append({"url": requested_url, "error": "Crawl4AI returned no result for this URL"})
            continue
        try:
            documents[requested_url] = _crawl_document(requested_url, result, settings)
        except Exception as exc:  # noqa: BLE001 - each URL must expose its own crawl failure.
            failures.append({"url": requested_url, "error": str(exc)})
    return documents, failures


def crawl_html_documents_sync(
    urls: list[str], settings: WebFetchSettings
) -> tuple[dict[str, FetchedDocument], list[dict[str, str]]]:
    return asyncio.run(crawl_html_documents(urls, settings))


def serper_scrape_document(
    url: str,
    settings: WebFetchSettings,
    *,
    api_key: str,
) -> FetchedDocument:
    """Fetch one public webpage through Serper's hosted Markdown scraper."""
    requested_url = canonical_url(url)
    _require_public_host(requested_url)
    if not str(api_key or "").strip():
        raise ValueError("SERPER_API_KEY is required for the Serper web fetch provider")
    response = httpx.post(
        settings.serper_scrape_url,
        json={"url": requested_url, "includeMarkdown": True},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        timeout=settings.timeout_seconds,
    )
    response.raise_for_status()
    if len(response.content) > settings.max_response_bytes:
        raise ValueError(f"Webpage response exceeds the configured {settings.max_response_bytes} byte limit")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Serper Scrape returned a non-object response")
    markdown = str(payload.get("markdown") or payload.get("text") or "").strip()
    metadata = payload.get("metadata")
    title_value = metadata.get("title", "") if isinstance(metadata, dict) else ""
    fallback_title = urlsplit(requested_url).path.rsplit("/", 1)[-1] or urlsplit(requested_url).hostname or "Web source"
    title = " ".join(str(title_value or fallback_title).split())
    if markdown and not markdown.lstrip().startswith("#"):
        markdown = f"# {title}\n\n{markdown}"
    visible_chars = len(re.sub(r"\s+", "", markdown))
    if visible_chars < settings.min_content_chars:
        raise ValueError(f"Extracted webpage content is too short ({visible_chars} characters) to use as evidence")
    return FetchedDocument(
        requested_url=requested_url,
        final_url=requested_url,
        title=title,
        content_type="application/json",
        raw_bytes=response.content,
        raw_suffix=".json",
        markdown=markdown + "\n",
        fetch_method="serper_scrape",
    )


def _pdf_to_markdown(content: bytes, *, fallback_title: str) -> tuple[str, str]:
    reader = PdfReader(BytesIO(content))
    metadata_title = str((reader.metadata.title if reader.metadata else "") or "").strip()
    title = metadata_title or fallback_title or "PDF source"
    sections = [f"# {title}"]
    for page_number, page in enumerate(reader.pages, start=1):
        text = str(page.extract_text() or "").strip()
        if text:
            sections.extend([f"## Page {page_number}", text])
    return title, "\n\n".join(sections).strip() + "\n"


def fetch_document(
    url: str,
    settings: WebFetchSettings,
    *,
    client: httpx.Client | None = None,
    validate_network: bool = True,
) -> FetchedDocument:
    requested_url = canonical_url(url)
    if validate_network:
        _require_public_host(requested_url)
    owns_client = client is None
    active_client = client or httpx.Client(
        follow_redirects=True,
        timeout=settings.timeout_seconds,
        headers={"User-Agent": settings.user_agent, "Accept": "text/html,application/pdf,text/plain"},
    )
    try:
        deadline = time.monotonic() + settings.timeout_seconds
        content_parts: list[bytes] = []
        content_size = 0
        with active_client.stream("GET", requested_url) as response:
            response.raise_for_status()
            final_url = canonical_url(str(response.url))
            content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
            encoding = response.encoding or "utf-8"
            for part in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError(
                        f"Webpage response exceeded the configured {settings.timeout_seconds:g} second total timeout"
                    )
                content_size += len(part)
                if content_size > settings.max_response_bytes:
                    raise ValueError(
                        f"Webpage response exceeds the configured {settings.max_response_bytes} byte limit"
                    )
                content_parts.append(part)
    finally:
        if owns_client:
            active_client.close()
    if validate_network:
        _require_public_host(final_url)
    content = b"".join(content_parts)
    if not content:
        raise ValueError("Webpage response body is empty")
    fallback_title = urlsplit(final_url).path.rsplit("/", 1)[-1] or urlsplit(final_url).hostname or "Web source"
    if content_type == "application/pdf" or content.startswith(b"%PDF-"):
        title, markdown = _pdf_to_markdown(content, fallback_title=fallback_title)
        suffix = ".pdf"
        normalized_type = "application/pdf"
    elif content_type.startswith("text/plain"):
        text = content.decode(encoding, errors="replace").strip()
        title = fallback_title
        markdown = f"# {title}\n\n{text}\n"
        suffix = ".txt"
        normalized_type = content_type or "text/plain"
    elif content_type in {"", "text/html", "application/xhtml+xml"} or b"<html" in content[:1024].lower():
        raise ValueError("HTML pages must be fetched through Crawl4AI batch rendering")
    else:
        raise ValueError(f"Unsupported webpage content type: {content_type or 'unknown'}")
    visible_chars = len(re.sub(r"\s+", "", markdown))
    if visible_chars < settings.min_content_chars:
        raise ValueError(
            f"Extracted webpage content is too short ({visible_chars} characters) to use as evidence"
        )
    return FetchedDocument(
        requested_url=requested_url,
        final_url=final_url,
        title=title,
        content_type=normalized_type,
        raw_bytes=content,
        raw_suffix=suffix,
        markdown=markdown,
        fetch_method="http_pdf" if normalized_type == "application/pdf" else "http_text",
    )


def _heading_before(markdown: str, position: int) -> str:
    headings = re.findall(r"(?m)^(#{1,6})\s+(.+?)\s*$", markdown[:position])
    return headings[-1][1].strip() if headings else ""


def chunk_markdown(source_id: str, markdown: str, settings: WebFetchSettings) -> list[dict[str, Any]]:
    content = str(markdown or "").strip()
    if not content:
        raise ValueError("Cleaned webpage content is empty")
    chunks: list[dict[str, Any]] = []
    for item in token_chunks(
        content,
        target_tokens=settings.schema_chunk_target_tokens,
        overlap_tokens=settings.schema_chunk_overlap_tokens,
    ):
        start = int(item["start"])
        text = str(item["text"])
        chunks.append(
            {
                "source_id": source_id,
                "chunk_id": f"{source_id}#chunk_{len(chunks) + 1:04d}",
                "heading": _heading_before(content, start),
                **item,
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return chunks


def search_tokens(value: str) -> list[str]:
    text = str(value or "").casefold()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text)
    expanded: list[str] = []
    for token in tokens:
        if token in _RETRIEVAL_STOPWORDS:
            continue
        normalized = token
        if re.fullmatch(r"[a-z]+", normalized) and len(normalized) > 4:
            if normalized.endswith("ies") and len(normalized) > 5:
                normalized = normalized[:-3] + "y"
            else:
                if normalized.endswith("s") and not normalized.endswith("ss"):
                    normalized = normalized[:-1]
                for suffix in ("ment", "ing", "ed"):
                    if normalized.endswith(suffix) and len(normalized) > len(suffix) + 3:
                        normalized = normalized[: -len(suffix)]
                        if normalized.endswith("v"):
                            normalized += "e"
                        break
        expanded.append(normalized)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 1:
            expanded.extend(token[index : index + 2] for index in range(len(token) - 1))
    return expanded


def rank_chunks(query: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = [dict(item) for item in chunks if isinstance(item, dict) and str(item.get("text") or "").strip()]
    query_terms = Counter(search_tokens(query))
    documents = [search_tokens(str(item["text"])) for item in ranked]
    if not query_terms or not documents:
        for item in ranked:
            item["score"] = 0.0
        return ranked
    average_length = sum(map(len, documents)) / len(documents)
    document_frequency: Counter[str] = Counter()
    for document in documents:
        document_frequency.update(set(document))
    for item, document in zip(ranked, documents):
        frequencies = Counter(document)
        score = 0.0
        for term, query_weight in query_terms.items():
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(
                1 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.5 * (0.25 + 0.75 * len(document) / max(average_length, 1))
            score += query_weight * inverse_frequency * frequency * 2.5 / denominator
        item["score"] = round(score, 6)
    ranked.sort(key=lambda item: float(item.get("score") or 0), reverse=True)
    return ranked


def relevant_excerpt(query: str, text: str, *, max_chars: int) -> str:
    content = str(text or "").strip()
    if max_chars < 1:
        raise ValueError("Relevant excerpt length must be positive")
    if len(content) <= max_chars:
        return content
    blocks = [block.strip() for block in re.split(r"\n{2,}", content) if block.strip()]
    windows: list[dict[str, Any]] = []
    for start in range(len(blocks)):
        selected: list[str] = []
        length = 0
        for block in blocks[start:]:
            addition = len(block) + (2 if selected else 0)
            if selected and length + addition > max_chars:
                break
            selected.append(block[:max_chars] if not selected else block)
            length += addition
            if length >= max_chars:
                break
        if selected:
            windows.append({"text": "\n\n".join(selected)[:max_chars], "start": start})
    ranked = rank_chunks(query, windows)
    best = ranked[0] if ranked and float(ranked[0].get("score") or 0) > 0 else windows[0]
    excerpt = str(best["text"]).strip()
    prefix = "...\n\n" if int(best.get("start") or 0) > 0 else ""
    suffix = "\n\n..." if excerpt and not content.endswith(excerpt) else ""
    return prefix + excerpt + suffix


def relevant_chunks(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    top_k: int,
    preferred_chunk_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    ranked = rank_chunks(query, chunks)
    preferred = set(preferred_chunk_ids or set())
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    if preferred:
        preferred_candidates = [
            dict(item)
            for item in chunks
            if str(item.get("chunk_id") or "") in preferred
        ]
        if preferred_candidates:
            return rank_chunks(query, preferred_candidates)[: max(1, top_k)]
    for item in ranked:
        chunk_id = str(item.get("chunk_id") or "")
        if chunk_id and chunk_id not in seen:
            selected.append(item)
            seen.add(chunk_id)
        if len(selected) >= max(1, top_k):
            break
    return selected[: max(1, top_k)]


def ranked_search_urls(query: str, results: list[dict[str, Any]]) -> list[str]:
    query_terms = set(search_tokens(query))
    candidates: list[tuple[float, int, str, str]] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        try:
            url = canonical_url(str(item.get("link") or item.get("url") or ""))
        except ValueError:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        candidate_terms = set(search_tokens(f"{item.get('title') or ''} {item.get('snippet') or ''}"))
        relevance = len(query_terms.intersection(candidate_terms)) / max(1, len(query_terms))
        domain = str(urlsplit(url).hostname or "")
        candidates.append((relevance, index, domain, url))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    diverse: list[str] = []
    repeated: list[str] = []
    seen_domains: set[str] = set()
    for _score, _index, domain, url in candidates:
        if domain not in seen_domains:
            diverse.append(url)
            seen_domains.add(domain)
        else:
            repeated.append(url)
    return [*diverse, *repeated]
