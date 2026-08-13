from __future__ import annotations

import json
import os
import ssl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.session_context import harness_session_environment
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout, SessionPaths
from knowcoder_workspace_builder.storage.sources import SourceRepository
from knowcoder_workspace_builder.tools import web_fetch as web_fetch_module
from knowcoder_workspace_builder.tools import web_content as web_content_module
from knowcoder_workspace_builder.tools.web_content import (
    FetchedDocument,
    WebFetchSettings,
    canonical_url,
    chunk_markdown,
    fetch_document,
    rank_chunks,
    ranked_search_urls,
    relevant_chunks,
    relevant_excerpt,
)


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/report")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


def test_web_fetch_retries_transient_transport_errors_but_not_forbidden_responses() -> None:
    assert web_fetch_module._is_retryable_web_fetch_error(httpx.ReadTimeout("timed out")) is True
    assert web_fetch_module._is_retryable_web_fetch_error(ssl.SSLError("connection closed")) is True
    assert web_fetch_module._is_retryable_web_fetch_error(_http_status_error(503)) is True
    assert web_fetch_module._is_retryable_web_fetch_error(_http_status_error(403)) is False


def test_web_fetch_retry_limit_is_one() -> None:
    assert web_fetch_module.WEB_FETCH_MAX_RETRIES == 1


def test_fetch_document_extracts_readable_html_without_navigation() -> None:
    body = " ".join(["Complete primary-source evidence about quarterly revenue."] * 12)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Official Filing</title></head><body>"
                "<nav>Navigation should disappear</nav>"
                f"<div role='main'><h1>Results</h1><ul><li><p>{body}</p></li></ul></div>"
                "<aside class='sphinxsidebar'><h3>Table of Contents</h3><ul><li>Results</li></ul></aside>"
                "<footer>Footer should disappear</footer>"
                "</body></html>"
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        document = fetch_document(
            "https://example.com/filing#section",
            WebFetchSettings(min_content_chars=80),
            client=client,
            validate_network=False,
        )
    finally:
        client.close()

    assert document.final_url == "https://example.com/filing"
    assert document.title == "Official Filing"
    assert "Complete primary-source evidence" in document.markdown
    assert document.markdown.count("Complete primary-source evidence") == 12
    assert "Navigation should disappear" not in document.markdown
    assert "Table of Contents" not in document.markdown
    assert "Footer should disappear" not in document.markdown


def test_fetch_document_enforces_total_response_deadline(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="Complete evidence body.",
        )

    clock = iter([0.0, 21.0])
    monkeypatch.setattr(web_content_module.time, "monotonic", clock.__next__)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        with pytest.raises(ValueError, match="total timeout"):
            fetch_document(
                "https://example.com/slow",
                WebFetchSettings(timeout_seconds=20, min_content_chars=10),
                client=client,
                validate_network=False,
            )
    finally:
        client.close()


def test_chunking_and_retrieval_preserve_chunk_identity() -> None:
    markdown = "# Report\n\n## Revenue\n\n" + ("Revenue increased in 2025. " * 120)
    markdown += "\n\n## Hiring\n\n" + ("The organization hired researchers. " * 120)
    settings = WebFetchSettings(chunk_target_chars=700, chunk_overlap_chars=80)

    chunks = chunk_markdown("crawl_test", markdown, settings)
    selected = relevant_chunks("2025 revenue increase", chunks, top_k=2)

    assert len(chunks) > 2
    assert len(selected) == 2
    assert all(item["source_id"] == "crawl_test" for item in selected)
    assert all(str(item["chunk_id"]).startswith("crawl_test#chunk_") for item in selected)
    assert any("Revenue" in item["text"] for item in selected)


def test_relevant_excerpt_selects_query_evidence_beyond_chunk_prefix() -> None:
    text = ("General background without schedule details.\n\n" * 30) + (
        "Alpha 1: October 13.\n\nBeta 4: July 18.\n\n"
        "Candidate 3: October 1.\n\nFinal release: October 7."
    )

    excerpt = relevant_excerpt("alpha beta candidate final release", text, max_chars=500)

    assert len(excerpt) <= 510
    assert "Alpha 1" in excerpt
    assert "Candidate 3" in excerpt
    assert "Final release" in excerpt


def test_chunk_ranking_prefers_factual_terms_over_source_boilerplate() -> None:
    chunks = [
        {"chunk_id": "intro", "text": "Official documentation source page for the release."},
        {
            "chunk_id": "facts",
            "text": "Runtime imports improved by one third and subprocess performance increased.",
        },
    ]

    ranked = rank_chunks("official documentation performance improvements", chunks)

    assert ranked[0]["chunk_id"] == "facts"


def test_preferred_chunks_are_still_ranked_for_the_current_question() -> None:
    chunks = [
        {"chunk_id": "intro", "text": "General release overview."},
        {"chunk_id": "optimization", "text": "Import time improved by one third."},
        {"chunk_id": "build", "text": "Build configuration details."},
    ]

    selected = relevant_chunks(
        "performance import time improvement",
        chunks,
        top_k=2,
        preferred_chunk_ids={"intro", "optimization", "build"},
    )

    assert selected[0]["chunk_id"] == "optimization"


def test_search_url_ranking_deduplicates_urls_and_prioritizes_distinct_domains() -> None:
    results = [
        {"title": "Revenue filing", "link": "https://a.example/report#top", "snippet": "2025 revenue"},
        {"title": "Duplicate", "link": "https://a.example/report", "snippet": "2025 revenue"},
        {"title": "Independent source", "link": "https://b.example/data", "snippet": "2025 revenue"},
        {"title": "Second A", "link": "https://a.example/analysis", "snippet": "revenue"},
    ]

    urls = ranked_search_urls("2025 revenue", results)

    assert urls == [
        "https://a.example/report",
        "https://b.example/data",
        "https://a.example/analysis",
    ]
    assert canonical_url("HTTPS://A.EXAMPLE:443/report#part") == "https://a.example/report"


@contextmanager
def _active_evidence_attempt(layout: ProjectLayout, paths: SessionPaths) -> Iterator[dict[str, Any]]:
    store = AttemptStore(layout)
    attempt = store.start(paths.session_id, "evidence", 1)
    stage_input = {
        "question": "What changed?",
        "steps": ["Collect primary evidence."],
        "upload_paths": [],
        "research_dir": "/.knowcoder_workspace/intermediate",
        "workspace_context": {"uncovered_step_indexes": [1]},
    }
    write_invocation_context(paths, attempt["attempt_id"], "evidence", stage_input)
    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            yield attempt
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    store.finish(paths.session_id, attempt["attempt_id"], "completed")


def test_fetch_and_store_pages_persists_complete_source_and_reuses_cache(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-web-content-1234", create=True)
    calls: list[str] = []
    events: list[dict[str, Any]] = []

    def fake_fetch(url: str, settings: WebFetchSettings) -> FetchedDocument:
        calls.append(url)
        markdown = "# Official evidence\n\n" + ("Verified complete body text. " * 80)
        return FetchedDocument(
            requested_url=url,
            final_url=url,
            title="Official evidence",
            content_type="text/html",
            raw_bytes=b"<html><body>complete</body></html>",
            raw_suffix=".html",
            markdown=markdown,
        )

    monkeypatch.setattr(web_fetch_module, "fetch_document", fake_fetch)
    monkeypatch.setattr(web_fetch_module, "emit_worker_event", events.append)
    settings = WebFetchSettings(min_content_chars=20, chunk_target_chars=500, chunk_overlap_chars=50)
    with _active_evidence_attempt(layout, paths):
        first = web_fetch_module.fetch_and_store_pages(
            ["https://example.com/report"],
            query="verified evidence",
            settings=settings,
        )
        second = web_fetch_module.fetch_and_store_pages(
            ["https://example.com/report"],
            query="verified evidence",
            settings=settings,
        )

        records = [item for item in SourceRepository(paths).list() if item.get("source_kind") == "web_crawl"]
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["sources"][0]["cached"] is True
        assert len(calls) == 1
        assert len(records) == 1
        record = records[0]
        content_path = web_fetch_module.resolve_path(str(record["file_path"]))
        chunk_path = web_fetch_module.resolve_path(str(record["chunk_path"]))
        metadata_path = web_fetch_module.resolve_path(str(record["metadata_path"]))
        assert "Verified complete body text" in content_path.read_text(encoding="utf-8")
        assert len(chunk_path.read_text(encoding="utf-8").splitlines()) > 1
        assert json.loads(metadata_path.read_text(encoding="utf-8"))["chunk_count"] > 1
        assert [event["message"]["status"] for event in events] == ["running", "done", "running", "done"]
        assert "Indexing" in events[2]["message"]["content"]


def test_fetch_and_store_pages_reports_explicit_fetch_failures(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-web-failure-1234", create=True)

    def failed_fetch(url: str, settings: WebFetchSettings) -> FetchedDocument:
        del url, settings
        raise ValueError("The page did not contain usable content")

    monkeypatch.setattr(web_fetch_module, "fetch_document", failed_fetch)
    monkeypatch.setattr(web_fetch_module, "emit_worker_event", lambda _event: None)
    with _active_evidence_attempt(layout, paths):
        result = web_fetch_module.fetch_and_store_pages(
            ["https://example.com/report"],
            query="verified evidence",
            settings=WebFetchSettings(min_content_chars=20),
        )

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["sources"] == []
    assert result["failures"] == [
        {"url": "https://example.com/report", "error": "The page did not contain usable content"}
    ]


def test_same_webpage_content_change_creates_a_new_active_source_version(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-web-version-1234", create=True)
    bodies = iter(
        [
            "# Report\n\n" + ("Original verified evidence. " * 40),
            "# Report\n\n" + ("Updated verified evidence. " * 40),
        ]
    )

    def fake_fetch(url: str, settings: WebFetchSettings) -> FetchedDocument:
        del settings
        return FetchedDocument(
            requested_url=url,
            final_url=url,
            title="Report",
            content_type="text/html",
            raw_bytes=b"<html><body>report</body></html>",
            raw_suffix=".html",
            markdown=next(bodies),
        )

    monkeypatch.setattr(web_fetch_module, "fetch_document", fake_fetch)
    monkeypatch.setattr(web_fetch_module, "emit_worker_event", lambda _event: None)
    settings = WebFetchSettings(min_content_chars=20)
    with _active_evidence_attempt(layout, paths):
        first = web_fetch_module.fetch_and_store_pages(
            ["https://example.com/report"],
            query="first version",
            settings=settings,
        )
    with _active_evidence_attempt(layout, paths):
        second = web_fetch_module.fetch_and_store_pages(
            ["https://example.com/report"],
            query="updated version",
            settings=settings,
        )

    records = [item for item in SourceRepository(paths).list() if item.get("source_kind") == "web_crawl"]
    assert first["sources"][0]["source_id"] != second["sources"][0]["source_id"]
    assert len(records) == 2
    assert [item["status"] for item in records].count("active") == 1
    assert [item["status"] for item in records].count("superseded") == 1
    old = next(item for item in records if item["status"] == "superseded")
    current = next(item for item in records if item["status"] == "active")
    assert old["superseded_by"] == current["source_id"]
