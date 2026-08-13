"""Framework-independent webpage fetching, extraction, chunking, and ranking."""

from __future__ import annotations

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
from bs4 import BeautifulSoup
from pypdf import PdfReader


WEB_CONTENT_FORMAT_VERSION = 2
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
    chunk_target_chars: int = 4_000
    chunk_overlap_chars: int = 240
    relevant_chunks_per_source: int = 4
    relevant_excerpt_chars: int = 1_600
    successful_pages_per_search: int = 2
    max_concurrency: int = 4
    user_agent: str = "SchemaWorkspaceBuilder/0.1 (+research evidence fetcher)"


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    title: str
    content_type: str
    raw_bytes: bytes
    raw_suffix: str
    markdown: str


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


def _html_to_markdown(content: bytes, *, fallback_title: str) -> tuple[str, str]:
    soup = BeautifulSoup(content, "lxml")
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else fallback_title).split())
    for tag in soup.select(
        "script, style, noscript, svg, form, nav, footer, header, aside, "
        ".sphinxsidebar, .related, .contents, .toctree-wrapper, .headerlink"
    ):
        tag.decompose()
    root = soup.select_one("main, article, [role='main'], .body") or soup.body or soup
    lines: list[str] = []
    for element in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote"]):
        if element.name != "li" and element.find_parent(["li", "pre", "blockquote"]) is not None:
            continue
        if element.name == "li":
            strings = [
                str(value).strip()
                for value in element.find_all(string=True)
                if value.find_parent("li") is element
            ]
            text = " ".join(" ".join(strings).split())
        else:
            text = " ".join(element.get_text(" ", strip=True).split())
        if not text:
            continue
        if element.name and element.name.startswith("h"):
            level = int(element.name[1])
            rendered = f"{'#' * level} {text}"
        elif element.name == "li":
            rendered = f"- {text}"
        elif element.name == "blockquote":
            rendered = f"> {text}"
        else:
            rendered = text
        if not lines or lines[-1] != rendered:
            lines.append(rendered)
    if not lines:
        text = " ".join(root.get_text(" ", strip=True).split())
        if text:
            lines.append(text)
    heading = title or fallback_title or "Web source"
    markdown = f"# {heading}\n\n" + "\n\n".join(lines)
    return heading, markdown.strip() + "\n"


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
        title, markdown = _html_to_markdown(content, fallback_title=fallback_title)
        suffix = ".html"
        normalized_type = content_type or "text/html"
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
    )


def _heading_before(markdown: str, position: int) -> str:
    headings = re.findall(r"(?m)^(#{1,6})\s+(.+?)\s*$", markdown[:position])
    return headings[-1][1].strip() if headings else ""


def chunk_markdown(source_id: str, markdown: str, settings: WebFetchSettings) -> list[dict[str, Any]]:
    content = str(markdown or "").strip()
    if not content:
        raise ValueError("Cleaned webpage content is empty")
    target = settings.chunk_target_chars
    overlap = min(settings.chunk_overlap_chars, max(0, target // 3))
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(content):
        hard_end = min(len(content), start + target)
        end = hard_end
        if hard_end < len(content):
            boundary = content.rfind("\n\n", start + target // 2, hard_end)
            if boundary > start:
                end = boundary
        text = content[start:end].strip()
        if text:
            chunks.append(
                {
                    "source_id": source_id,
                    "chunk_id": f"{source_id}#chunk_{len(chunks) + 1:04d}",
                    "heading": _heading_before(content, start),
                    "start": start,
                    "end": end,
                    "text": text,
                    "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        if end >= len(content):
            break
        next_start = max(start + 1, end - overlap)
        paragraph = content.find("\n\n", next_start, min(len(content), end + overlap + 1))
        start = paragraph + 2 if paragraph >= 0 and paragraph + 2 < end else next_start
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
