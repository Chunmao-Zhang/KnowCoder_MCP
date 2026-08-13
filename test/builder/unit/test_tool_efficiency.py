from __future__ import annotations

import json
import os
import hashlib
import threading
import time
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from knowcoder_workspace_builder.runtime.agent_tool_call_middleware import (
    EvidenceManifestPreflightMiddleware,
    FailedToolCircuitBreakerMiddleware,
    RunAttemptGuardMiddleware,
    RunScopedFileToolMiddleware,
    VALIDATION_ROUND_ENV,
)
from knowcoder_workspace_builder.runtime.harness_worker import _load_workspace_snapshot
from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.session_context import (
    ATTEMPT_ID_ENV,
    SESSION_ID_ENV,
    SESSION_ROOT_ENV,
    harness_session_environment,
    active_session_paths,
)
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.tool_calls import SearchLedger, ToolCallLedger
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json
from knowcoder_workspace_builder.tools.evidence_retriever import web_search, web_search_batch
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import Stage
from knowcoder_workspace_builder.runtime.workspace_sources import register_source_record, source_records


evidence_module = import_module("knowcoder_workspace_builder.tools.evidence_retriever")


@pytest.fixture(autouse=True)
def _complete_web_fetch(monkeypatch):
    def fake_fetch(urls, *, query, target_successes=None, settings=None):
        del settings
        paths = active_session_paths()
        selected = list(dict.fromkeys(str(item) for item in urls if str(item)))
        target = min(len(selected), target_successes or len(selected))
        sources = []
        for url in selected[:target]:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
            source_id = f"crawl_{digest[:20]}"
            directory = paths.sources / "web_crawls" / source_id
            content_path = directory / "content.md"
            chunk_path = directory / "chunks.jsonl"
            AtomicWriter(paths).text(content_path, f"# Complete source\n\n{query}\n")
            AtomicWriter(paths).text(
                chunk_path,
                json.dumps(
                    {
                        "source_id": source_id,
                        "chunk_id": f"{source_id}#chunk_0001",
                        "heading": "Complete source",
                        "start": 0,
                        "end": len(query),
                        "text": query,
                        "content_sha256": digest,
                    }
                )
                + "\n",
            )
            register_source_record(
                paths.root,
                "web_crawls",
                {
                    "source_id": source_id,
                    "source_kind": "web_crawl",
                    "file_path": virtual_path_for(paths.root, content_path),
                    "chunk_path": virtual_path_for(paths.root, chunk_path),
                    "url": url,
                    "title": "Complete source",
                    "content_sha256": digest,
                    "chunk_count": 1,
                },
            )
            sources.append(
                {
                    "source_id": source_id,
                    "url": url,
                    "title": "Complete source",
                    "relevant_chunks": [
                        {
                            "source_id": source_id,
                            "chunk_id": f"{source_id}#chunk_0001",
                            "text": query,
                        }
                    ],
                }
            )
        refs = [
            {"source_id": source["source_id"], "chunk_id": source["relevant_chunks"][0]["chunk_id"]}
            for source in sources
        ]
        return {
            "ok": bool(sources),
            "status": "completed" if sources else "failed",
            "sources": sources,
            "failures": [],
            "coverage_binding": {
                "source_ids": [source["source_id"] for source in sources],
                "chunk_refs": refs,
            },
        }

    monkeypatch.setattr(evidence_module, "fetch_and_store_pages", fake_fetch)


def test_runtime_loads_workspace_snapshot_before_model_work(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-workspace-snapshot", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "schema_build", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "schema_build",
        {
            "question": "Build a Schema.",
            "steps": ["Collect records."],
            "data_manifest": {"coverage": [], "unresolved_gaps": []},
            "workspace_context": {},
        },
    )

    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            snapshot = _load_workspace_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["workspace_exists"] is False
    assert ToolCallLedger(paths, attempt["attempt_id"]).completed_count("workspace_readme_browser") == 1


def _environment(values: dict[str, str]):
    class Environment:
        def __enter__(self):
            self.previous = {name: os.environ.get(name) for name in values}
            os.environ.update(values)

        def __exit__(self, *_args):
            for name, value in self.previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    return Environment()


def test_evidence_input_covers_all_confirmed_steps(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, recover_interrupted=False)
    state = BuildState(
        session_id="session-dynamic-search-budget",
        question="Research every confirmed dimension.",
        upload_paths=[],
        stage=Stage.EVIDENCE,
        problem={"steps": [f"Research dimension {index}." for index in range(15)]},
        problem_confirmed=True,
    )

    stage_input = service.coordinator._stage_input(state, Stage.EVIDENCE)

    assert stage_input["workspace_context"]["uncovered_step_indexes"] == list(range(1, 16))


def test_evidence_input_exposes_only_new_or_changed_step_indexes(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, recover_interrupted=False)
    old_step = "Collect existing values."
    state = BuildState(
        session_id="session-revision-search-indexes",
        question="Expand the research.",
        upload_paths=[],
        stage=Stage.EVIDENCE,
        problem={"steps": ["Collect a new factor.", old_step, "Collect a changed period."]},
        problem_confirmed=True,
        evidence={
            "coverage": [
                {
                    "step": old_step,
                    "requirements": ["values"],
                    "status": "covered",
                    "source_ids": ["source-old"],
                }
            ],
            "sources": [{"source_id": "source-old"}],
        },
    )

    stage_input = service.coordinator._stage_input(state, Stage.EVIDENCE)

    assert stage_input["workspace_context"]["uncovered_step_indexes"] == [1, 3]


def test_evidence_input_accepts_large_step_lists(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, recover_interrupted=False)
    state = BuildState(
        session_id="session-excessive-search-budget",
        question="Research every confirmed dimension.",
        upload_paths=[],
        stage=Stage.EVIDENCE,
        problem={"steps": [f"Research dimension {index}." for index in range(201)]},
        problem_confirmed=True,
    )

    stage_input = service.coordinator._stage_input(state, Stage.EVIDENCE)
    assert len(stage_input["workspace_context"]["uncovered_step_indexes"]) == 201


def test_extractor_input_includes_compact_confirmed_requirements(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, recover_interrupted=False)
    step = "Compare every supported company revenue and margin."
    state = BuildState(
        session_id="session-extraction-scope",
        question="Compare companies.",
        upload_paths=[],
        stage=Stage.EXTRACT,
        problem={"steps": [step]},
        problem_confirmed=True,
        evidence={
            "coverage": [
                {
                    "step": step,
                    "requirements": ["Company revenue", "Company margin"],
                    "status": "covered",
                    "source_ids": ["source-a"],
                }
            ],
            "sources": [
                {
                    "source_id": "source-a",
                    "file_path": "/.knowcoder_workspace/intermediate/sources/source-a.md",
                    "source_kind": "web_search_bundle",
                    "title": "Company comparison",
                }
            ],
        },
        schema_review={"schema_source": "class Entity: ...", "schema_outline": {"entities": []}},
        schema_confirmed=True,
    )

    stage_input = service.coordinator._stage_input(state, Stage.EXTRACT)

    assert stage_input["workspace_context"]["confirmed_steps"] == [step]
    assert stage_input["workspace_context"]["confirmed_requirements"] == [
        {
            "step_index": 1,
            "requirements": ["Company revenue", "Company margin"],
            "status": "covered",
        }
    ]


def test_tool_call_ledger_rejects_an_equivalent_call_and_requires_objective(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-tools-ledger", create=True)
    ledger = ToolCallLedger(paths, "attempt-ledger")
    signature = ledger.start("source_reader", {"file_paths": ["/source.csv"]}, "Read assigned source")
    ledger.finish(signature, "completed")

    try:
        ledger.start("source_reader", {"file_paths": ["/source.csv"]}, "Read assigned source")
    except Exception as exc:
        assert "Equivalent tool call" in str(exc)
    else:
        raise AssertionError("Equivalent tool call was accepted")

    try:
        ledger.start("source_reader", {"file_paths": ["/other.csv"]}, "")
    except Exception as exc:
        assert "requires a current objective" in str(exc)
    else:
        raise AssertionError("Tool call without an objective was accepted")


def test_tool_call_ledger_allows_failed_retry_and_multiple_distinct_successes(
    runtime_project: Path,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-tools-ledger-retry", create=True)
    ledger = ToolCallLedger(paths, "attempt-ledger-retry")
    arguments = {"file_path": "/.knowcoder_workspace/intermediate/sources/parser.py"}

    first = ledger.start("execute_code", arguments, "Run structured parser")
    ledger.finish(first, "failed")
    second = ledger.start("execute_code", arguments, "Run corrected structured parser")
    ledger.finish(second, "completed")
    third = ledger.start(
        "execute_code",
        {"file_path": "/.knowcoder_workspace/intermediate/sources/other.py"},
        "Run another parser",
    )
    ledger.finish(third, "completed")


def test_equivalent_web_search_reuses_result_and_distinct_search_runs(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-ledger", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Compare records."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {},
        },
    )
    calls: list[dict] = []

    def fake_search(query: str, num_results: int):
        calls.append({"query": query, "num_results": num_results})
        return json.dumps(
            {
                "query": query,
                "results": [
                    {"title": "Result", "link": "https://example.test/primary"},
                    {"title": "Duplicate", "link": "https://example.test/primary"},
                ],
            }
        )

    monkeypatch.setattr(evidence_module, "_perform_search", fake_search)
    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            first = json.loads(
                web_search.invoke(
                    {
                        "query": "alpha beta",
                        "step_index": 1,
                        "purpose": "Cover comparison inputs.",
                        "expected_new_information": "A primary-source comparison fact.",
                    }
                )
            )
            equivalent = json.loads(
                web_search.invoke(
                    {
                        "query": "beta alpha",
                        "step_index": 1,
                        "purpose": "Confirm the same comparison inputs.",
                        "expected_new_information": "The same primary-source comparison fact.",
                    }
                )
            )
            distinct = json.loads(
                web_search.invoke(
                    {
                        "query": "gamma delta",
                        "step_index": 1,
                        "purpose": "Cover a distinct factor.",
                        "expected_new_information": "A distinct factor fact.",
                    }
                )
            )

            records = SearchLedger(paths, attempt["attempt_id"]).records()
            sources = source_records(paths.root)

    assert first["ok"] is True
    assert first["coverage_binding"]["step_index"] == 1
    assert len(first["coverage_binding"]["source_ids"]) == 1
    assert first["coverage_binding"]["source_ids"][0].startswith("crawl_")
    assert equivalent["cached"] is True
    assert distinct["ok"] is True
    assert distinct["cached"] is False
    assert len(calls) == 2
    assert len(records) == 2
    assert sources[0]["url"] == "https://example.test/primary"
    assert sources[0]["source_kind"] == "web_crawl"


def test_web_search_allows_first_pass_calls_in_any_step_order(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-order", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect records.", "Collect comparison factors."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {},
        },
    )

    def fake_search(query: str, num_results: int):
        return json.dumps(
            {"query": query, "results": [{"title": "Result", "link": f"https://example.test/{query}"}]}
        )

    monkeypatch.setattr(evidence_module, "_perform_search", fake_search)
    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            out_of_order = json.loads(
                web_search.invoke(
                    {
                        "query": "comparison factors",
                        "step_index": 2,
                        "purpose": "Cover the second step.",
                        "expected_new_information": "Comparison factors.",
                    }
                )
            )
            first = json.loads(
                web_search.invoke(
                    {
                        "query": "records",
                        "step_index": 1,
                        "purpose": "Cover the first step.",
                        "expected_new_information": "Record facts.",
                    }
                )
            )
            second = json.loads(
                web_search.invoke(
                    {
                        "query": "comparison factors",
                        "step_index": 2,
                        "purpose": "Cover the second step.",
                        "expected_new_information": "Comparison factors.",
                    }
                )
            )

    assert out_of_order["ok"] is True
    assert first["ok"] is True
    assert second["cached"] is True
    assert [item["step_index"] for item in SearchLedger(paths, attempt["attempt_id"]).records()] == [2, 1]


def test_web_search_orders_only_incremental_uncovered_steps(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-incremental-search-order", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Expand the current research.",
            "steps": ["Keep current data.", "Collect a new factor.", "Collect another new factor."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [2, 3]},
        },
    )

    monkeypatch.setattr(
        evidence_module,
        "_perform_search",
        lambda query, num_results: json.dumps(
            {"query": query, "results": [{"title": "Result", "link": f"https://example.test/{query}"}]}
        ),
    )
    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            second = json.loads(
                web_search.invoke(
                    {
                        "query": "new factor",
                        "step_index": 2,
                        "purpose": "Cover the first new step.",
                        "expected_new_information": "New factor facts.",
                    }
                )
            )
            third = json.loads(
                web_search.invoke(
                    {
                        "query": "another new factor",
                        "step_index": 3,
                        "purpose": "Cover the second new step.",
                        "expected_new_information": "Additional factor facts.",
                    }
                )
            )

    assert second["ok"] is True
    assert third["ok"] is True
    assert [item["step_index"] for item in SearchLedger(paths, attempt["attempt_id"]).records()] == [2, 3]


def test_web_search_allows_another_step_after_a_failed_first_pass(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-retry-order", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Collect two factors.",
            "steps": ["Collect the first factor.", "Collect the second factor."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1, 2]},
        },
    )
    responses = iter(
        [
            json.dumps({"error": "temporary search failure"}),
            json.dumps({"results": [{"title": "Second-step result", "link": "https://example.test/second"}]}),
            json.dumps({"results": [{"title": "Recovered result", "link": "https://example.test/recovered"}]}),
        ]
    )
    monkeypatch.setattr(evidence_module, "_perform_search", lambda query, num_results: next(responses))

    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            failed = json.loads(
                web_search.invoke(
                    {
                        "query": "first factor initial",
                        "step_index": 1,
                        "purpose": "Cover the first step.",
                        "expected_new_information": "First factor facts.",
                    }
                )
            )
            out_of_order = json.loads(
                web_search.invoke(
                    {
                        "query": "second factor",
                        "step_index": 2,
                        "purpose": "Cover the second step.",
                        "expected_new_information": "Second factor facts.",
                    }
                )
            )
            recovered = json.loads(
                web_search.invoke(
                    {
                        "query": "first factor corrected",
                        "step_index": 1,
                        "purpose": "Retry the first step.",
                        "expected_new_information": "First factor facts.",
                    }
                )
            )

    assert failed["ok"] is False
    assert out_of_order["ok"] is True
    assert recovered["ok"] is True


def test_web_search_batch_accepts_multiple_steps_without_order_validation(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-batch", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Collect two factors.",
            "steps": ["Collect the first factor.", "Collect the second factor."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1, 2]},
        },
    )
    monkeypatch.setattr(
        evidence_module,
        "_perform_search",
        lambda query, num_results: json.dumps(
            {"query": query, "results": [{"title": query, "link": f"https://example.test/{query}"}]}
        ),
    )

    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            result = json.loads(
                web_search_batch.invoke(
                    {
                        "searches": [
                            {
                                "query": "second factor",
                                "step_index": 2,
                                "purpose": "Cover the second step.",
                                "expected_new_information": "Second factor facts.",
                            },
                            {
                                "query": "first factor",
                                "step_index": 1,
                                "purpose": "Cover the first step.",
                                "expected_new_information": "First factor facts.",
                            },
                        ]
                    }
                )
            )

    assert result["ok"] is True
    assert result["completed"] == 2
    assert sorted(item["step_index"] for item in SearchLedger(paths, attempt["attempt_id"]).records()) == [1, 2]
    assert [item["step_index"] for item in result["results"]] == [2, 1]


def test_web_search_batch_runs_independent_searches_concurrently_and_preserves_order(monkeypatch) -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def invoke(search):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return json.dumps({"ok": True, "step_index": search["step_index"]})

    monkeypatch.setattr(evidence_module, "web_search", SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(
        evidence_module,
        "load_web_fetch_settings",
        lambda: SimpleNamespace(max_concurrency=3),
    )

    result = json.loads(
        web_search_batch.invoke(
            {
                "searches": [
                    {"step_index": 3},
                    {"step_index": 1},
                    {"step_index": 2},
                ]
            }
        )
    )

    assert peak == 3
    assert [item["step_index"] for item in result["results"]] == [3, 1, 2]
    assert [item["position"] for item in result["results"]] == [1, 2, 3]


def test_web_search_batch_keeps_successes_and_identifies_only_failed_searches(monkeypatch) -> None:
    def invoke(search):
        if search["step_index"] == 2:
            return json.dumps(
                {
                    "ok": False,
                    "step_index": 2,
                    "error_type": "external_search_error",
                    "error": "Source rejected automated access.",
                }
            )
        return json.dumps({"ok": True, "step_index": search["step_index"]})

    monkeypatch.setattr(evidence_module, "web_search", SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(evidence_module, "load_web_fetch_settings", lambda: SimpleNamespace(max_concurrency=3))

    result = json.loads(
        web_search_batch.invoke(
            {"searches": [{"step_index": 1}, {"step_index": 2}, {"step_index": 3}]}
        )
    )

    assert result["ok"] is True
    assert result["status"] == "partial"
    assert result["completed"] == 2
    assert result["failed"] == 1
    assert result["failed_searches"] == [
        {
            "position": 2,
            "step_index": 2,
            "error_type": "external_search_error",
            "error": "Source rejected automated access.",
        }
    ]
    assert "Retry only the failed searches" in result["next_action"]


def test_web_search_rejects_empty_result_bundles(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-empty-result", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "evidence",
        {
            "question": "Collect one factor.",
            "steps": ["Collect the factor."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1]},
        },
    )
    responses = iter(
        [
            json.dumps({"results": []}),
            json.dumps({"results": [{"title": "Primary result", "link": "https://example.test/factor"}]}),
        ]
    )
    monkeypatch.setattr(evidence_module, "_perform_search", lambda query, num_results: next(responses))

    with harness_session_environment(paths, attempt["attempt_id"]) as environment:
        with _environment(dict(environment)):
            empty = json.loads(
                web_search.invoke(
                    {
                        "query": "factor broad query",
                        "step_index": 1,
                        "purpose": "Cover the factor.",
                        "expected_new_information": "A grounded factor fact.",
                    }
                )
            )
            corrected = json.loads(
                web_search.invoke(
                    {
                        "query": "factor primary source",
                        "step_index": 1,
                        "purpose": "Retry the factor.",
                        "expected_new_information": "A grounded factor fact.",
                    }
                )
            )
            registered = source_records(paths.root)

    records = SearchLedger(paths, attempt["attempt_id"]).records()
    assert empty["ok"] is False
    assert empty["error_type"] == "external_search_error"
    assert corrected["ok"] is True
    assert [item["status"] for item in records] == ["failed", "completed"]
    assert [item["source_id"] for item in registered] == [
        corrected["coverage_binding"]["source_ids"][0]
    ]


def test_run_attempt_guard_records_terminal_calls_and_rejects_invalid_calls(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-middleware-ledger", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect the supplied record."],
            "upload_paths": ["/.knowcoder_workspace/intermediate/sources/source.csv"],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    middleware = RunAttemptGuardMiddleware()
    handled: list[str] = []

    request = ToolCallRequest(
        tool_call={"id": "call-success", "name": "source_reader", "args": {"file_paths": ["/source.csv"]}},
        tool=None,
        state={},
        runtime=None,
    )

    def successful(current: ToolCallRequest) -> ToolMessage:
        handled.append(current.tool_call["id"])
        return ToolMessage(content='{"ok": true}', name="source_reader", tool_call_id=current.tool_call["id"])

    result = middleware.wrap_tool_call(request, successful)
    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert handled == ["call-success"]

    duplicate = middleware.wrap_tool_call(request.override(tool_call={**request.tool_call, "id": "call-duplicate"}), successful)
    assert isinstance(duplicate, ToolMessage)
    assert duplicate.status == "error"
    assert "Equivalent tool call" in str(duplicate.content)
    assert handled == ["call-success"]

    objective_missing = ToolCallRequest(
        tool_call={"id": "call-no-objective", "name": "unknown_tool", "args": {}},
        tool=None,
        state={},
        runtime=None,
    )
    missing = middleware.wrap_tool_call(objective_missing, successful)
    assert isinstance(missing, ToolMessage)
    assert missing.status == "error"
    assert "requires a current objective" in str(missing.content)
    assert handled == ["call-success"]

    failing = ToolCallRequest(
        tool_call={"id": "call-failed", "name": "source_reader", "args": {"file_paths": ["/other.csv"]}},
        tool=None,
        state={},
        runtime=None,
    )
    with pytest.raises(RuntimeError, match="source unavailable"):
        middleware.wrap_tool_call(failing, lambda _request: (_ for _ in ()).throw(RuntimeError("source unavailable")))

    ledger_path = paths.attempts / attempt_id / "tool_calls.json"
    records = read_json(ledger_path)["calls"]
    assert [record["status"] for record in records] == ["completed", "failed"]
    assert all(record["finished_at"] for record in records)


def test_run_attempt_guard_declares_objectives_for_file_persistence_tools() -> None:
    objectives = RunAttemptGuardMiddleware._OBJECTIVES
    for tool_name in (
        "save_problem_review",
        "save_evidence_manifest",
        "save_schema",
        "save_schema_judgement",
        "save_workspace_readme",
        "extract_unstructured_chunks",
    ):
        assert objectives.get(tool_name)


def test_run_attempt_guard_rejects_search_for_an_already_covered_step(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-scope-guard", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Expand the current research.",
            "steps": ["Retain covered records.", "Collect the new factor."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [2]},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    middleware = RunAttemptGuardMiddleware()
    handled: list[int] = []

    def search(current: ToolCallRequest) -> ToolMessage:
        handled.append(current.tool_call["args"]["step_index"])
        return ToolMessage(
            content='{"ok": true}',
            name="web_search",
            tool_call_id=current.tool_call["id"],
        )

    covered = ToolCallRequest(
        tool_call={
            "id": "search-covered",
            "name": "web_search",
            "args": {
                "query": "covered records",
                "step_index": 1,
                "purpose": "Review covered records.",
            },
        },
        tool=None,
        state={},
        runtime=None,
    )
    rejected = middleware.wrap_tool_call(covered, search)

    assert isinstance(rejected, ToolMessage)
    assert rejected.status == "error"
    assert "outside the current uncovered research steps" in str(rejected.content)
    assert handled == []

    uncovered = covered.override(
        tool_call={
            **covered.tool_call,
            "id": "search-uncovered",
            "args": {**covered.tool_call["args"], "step_index": 2},
        }
    )
    accepted = middleware.wrap_tool_call(uncovered, search)

    assert accepted.status == "success"
    assert handled == [2]


def test_run_attempt_guard_reopens_research_after_manifest_persistence_fails(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-after-manifest", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect records."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1]},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    ledger = ToolCallLedger(paths, attempt_id)
    signature = ledger.start("save_evidence_manifest", {"coverage": []}, "Persist evidence manifest")
    ledger.finish(signature, "failed")
    request = ToolCallRequest(
        tool_call={
            "id": "late-search",
            "name": "web_search",
            "args": {
                "query": "new search",
                "step_index": 1,
                "purpose": "Collect records.",
                "expected_new_information": "Comparable values.",
            },
        },
        tool=None,
        state={},
        runtime=None,
    )

    handled: list[str] = []

    def search(current: ToolCallRequest) -> ToolMessage:
        handled.append(current.tool_call["id"])
        return ToolMessage(
            content='{"ok": true}',
            name="web_search",
            tool_call_id=current.tool_call["id"],
        )

    result = RunAttemptGuardMiddleware().wrap_tool_call(request, search)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert handled == ["late-search"]


def test_run_attempt_guard_allows_research_after_manifest_persistence_succeeds(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-search-after-valid-manifest", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect records."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1]},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    ledger = ToolCallLedger(paths, attempt_id)
    signature = ledger.start("save_evidence_manifest", {"coverage": []}, "Persist evidence manifest")
    ledger.finish(signature, "completed")
    request = ToolCallRequest(
        tool_call={
            "id": "search-after-success",
            "name": "web_search",
            "args": {
                "query": "new search",
                "step_index": 1,
                "purpose": "Collect records.",
                "expected_new_information": "Comparable values.",
            },
        },
        tool=None,
        state={},
        runtime=None,
    )

    handled: list[str] = []

    def search(current: ToolCallRequest) -> ToolMessage:
        handled.append(current.tool_call["id"])
        return ToolMessage(
            content='{"ok": true}',
            name="web_search",
            tool_call_id=current.tool_call["id"],
        )

    result = RunAttemptGuardMiddleware().wrap_tool_call(request, search)

    assert isinstance(result, ToolMessage)
    assert result.status == "success"
    assert handled == ["search-after-success"]


def test_evidence_manifest_can_be_resaved_after_research_revision(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-evidence-resave", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect records."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1]},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    middleware = RunAttemptGuardMiddleware()
    arguments = {
        "coverage": [{"step_index": 1, "status": "covered"}],
        "unresolved_gaps": [],
    }

    def save(current: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content='{"ok": true}',
            name="save_evidence_manifest",
            tool_call_id=current.tool_call["id"],
        )

    first = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "save-1", "name": "save_evidence_manifest", "args": arguments},
            tool=None,
            state={},
            runtime=None,
        ),
        save,
    )
    unchanged = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "save-2", "name": "save_evidence_manifest", "args": arguments},
            tool=None,
            state={},
            runtime=None,
        ),
        save,
    )
    SearchLedger(paths, attempt_id).append(
        {
            "signature": "supplement",
            "step_index": 1,
            "status": "completed",
            "response": {"coverage_binding": {"source_ids": ["source-new"]}},
        }
    )
    revised = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "save-3", "name": "save_evidence_manifest", "args": arguments},
            tool=None,
            state={},
            runtime=None,
        ),
        save,
    )

    assert first.status == "success"
    assert unchanged.status == "error"
    assert "Equivalent tool call" in str(unchanged.content)
    assert revised.status == "success"


def test_unlimited_search_guidance_converges_after_one_bundle_per_step(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-first-pass-convergence", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "evidence",
        {
            "question": "Compare records.",
            "steps": ["Collect values.", "Collect factors."],
            "upload_paths": [],
            "research_dir": "/.knowcoder_workspace/intermediate",
            "workspace_context": {"uncovered_step_indexes": [1, 2]},
        },
    )
    search_ledger = SearchLedger(paths, attempt_id)
    for step_index in (1, 2):
        search_ledger.append(
            {
                "signature": f"step-{step_index}",
                "step_index": step_index,
                "status": "completed",
                "response": {"coverage_binding": {"step_index": step_index, "source_ids": []}},
            }
        )
    request = SimpleNamespace(
        messages=[],
        override=lambda **changes: SimpleNamespace(messages=changes["messages"]),
    )

    with harness_session_environment(paths, attempt_id) as environment:
        with _environment(dict(environment)):
            guided = FailedToolCircuitBreakerMiddleware(["web_search"])._guide_search_completion(request)

    assert isinstance(guided.messages[-1], SystemMessage)
    assert "successful first-pass source bundle" in guided.messages[-1].content
    assert "supplemental searches for each high-impact unsupported claim" in guided.messages[-1].content
    assert "save the manifest" in guided.messages[-1].content.casefold()


def test_unstructured_reader_requires_previous_batch_persistence(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-reader-persistence", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "extract", 1)
    attempt_id = attempt["attempt_id"]
    write_invocation_context(
        paths,
        attempt_id,
        "extract",
        {
            "schema_outline": {"entities": []},
            "sources": [],
            "draft_path": "/.knowcoder_workspace/intermediate/attempts/draft.json",
            "workspace_context": {},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt_id)
    middleware = RunAttemptGuardMiddleware()

    def success(current: ToolCallRequest) -> ToolMessage:
        return ToolMessage(content='{"ok": true}', name=current.tool_call["name"], tool_call_id=current.tool_call["id"])

    first = ToolCallRequest(
        tool_call={"id": "read-1", "name": "source_reader", "args": {"file_paths": ["/source-1"]}},
        tool=None,
        state={},
        runtime=None,
    )
    assert middleware.wrap_tool_call(first, success).status == "success"

    pending = middleware.wrap_tool_call(
        first.override(
            tool_call={"id": "read-2", "name": "source_reader", "args": {"file_paths": ["/source-2"]}}
        ),
        success,
    )
    assert pending.status == "error"
    assert "Persist the current unstructured source batch" in str(pending.content)

    monkeypatch.setenv(VALIDATION_ROUND_ENV, "2")
    repair_read = middleware.wrap_tool_call(
        first.override(
            tool_call={"id": "read-repair", "name": "source_reader", "args": {"file_paths": ["/source-1"]}}
        ),
        success,
    )
    assert repair_read.status == "success"

    append = first.override(
        tool_call={
            "id": "append-1",
            "name": "append_instances_batch",
            "args": {"processed_source_ids": ["source-1"]},
        }
    )
    assert middleware.wrap_tool_call(append, success).status == "success"
    assert middleware.wrap_tool_call(
        first.override(
            tool_call={"id": "read-3", "name": "source_reader", "args": {"file_paths": ["/source-2"]}}
        ),
        success,
    ).status == "success"


def test_structured_file_tools_require_append_after_each_successful_execution(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-structured-sequence", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "structured_extract", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "structured_extract",
        {
            "schema_outline": {"entities": []},
            "sources": [],
            "draft_path": "/.knowcoder_workspace/intermediate/attempts/structured_draft.json",
                "work_dir": "/.knowcoder_workspace/intermediate/sources",
                "batch_path": "/.knowcoder_workspace/intermediate/attempts/structured_batches.json",
            "workspace_context": {},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt["attempt_id"])
    ledger = ToolCallLedger(paths, attempt["attempt_id"])
    execution = ledger.start("execute_code", {"file_path": "/parser.py"}, "run parser")
    ledger.finish(execution, "completed")
    middleware = RunAttemptGuardMiddleware()
    blocked = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "debug", "name": "write_file", "args": {"file_path": "/debug.py"}},
            tool=None,
            state={},
            runtime=None,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("file action should be blocked")),
    )
    assert blocked.status == "error"
    assert "append_instances_batches_from_file" in str(blocked.content)

    append = ledger.start(
        "append_instances_batches_from_file",
        {"file_path": "/batch.json"},
        "validate batch",
    )
    ledger.finish(append, "failed")
    allowed = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "repair", "name": "write_file", "args": {"file_path": "/repair.py"}},
            tool=None,
            state={},
            runtime=None,
        ),
        lambda request: ToolMessage(content='{"ok": true}', name="write_file", tool_call_id=request.tool_call["id"]),
    )
    assert allowed.status == "success"


def test_structured_append_allows_a_repaired_batch_revision(
    runtime_project: Path,
    monkeypatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-structured-repair", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "structured_extract", 1)
    write_invocation_context(
        paths,
        attempt["attempt_id"],
        "structured_extract",
        {
            "schema_outline": {"entities": []},
            "sources": [],
            "draft_path": "/.knowcoder_workspace/intermediate/attempts/structured_draft.json",
            "work_dir": "/.knowcoder_workspace/intermediate/sources",
            "batch_path": "/.knowcoder_workspace/intermediate/attempts/structured_batches.json",
            "workspace_context": {},
        },
    )
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt["attempt_id"])
    batch_path = paths.attempts / attempt["attempt_id"] / "structured_batches.json"
    AtomicWriter(paths).json(batch_path, {"batches": []})
    middleware = RunAttemptGuardMiddleware()

    def append(current: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content='{"ok": true}',
            name="append_instances_batches_from_file",
            tool_call_id=current.tool_call["id"],
        )

    def request(call_id: str) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={
                "id": call_id,
                "name": "append_instances_batches_from_file",
                "args": {},
            },
            tool=None,
            state={},
            runtime=None,
        )

    assert middleware.wrap_tool_call(request("append-1"), append).status == "success"
    duplicate = middleware.wrap_tool_call(request("append-2"), append)
    assert duplicate.status == "error"
    assert "Equivalent tool call" in str(duplicate.content)

    AtomicWriter(paths).json(batch_path, {"batches": [{"entities": [], "relations": []}]})
    assert middleware.wrap_tool_call(request("append-3"), append).status == "success"


def test_structured_write_file_only_accepts_python_scripts(runtime_project: Path, monkeypatch) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-structured-write", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "structured_extract", 1)
    monkeypatch.setenv(SESSION_ROOT_ENV, str(paths.root))
    monkeypatch.setenv(SESSION_ID_ENV, paths.session_id)
    monkeypatch.setenv(ATTEMPT_ID_ENV, attempt["attempt_id"])
    middleware = RunScopedFileToolMiddleware(allowed_subdirs=["intermediate"], execute_subdirs=["intermediate"])
    blocked = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={
                "id": "overwrite-batch",
                "name": "write_file",
                "args": {"file_path": "/.knowcoder_workspace/intermediate/sources/structured_batches.json"},
            },
            tool=None,
            state={},
            runtime=None,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("JSON overwrite should be blocked")),
    )
    assert blocked.status == "error"
    assert "Python parsing script" in str(blocked.content)


def test_failed_tool_circuit_allows_two_external_retries_then_opens() -> None:
    first = ToolMessage(
        content='{"ok": false, "error_type": "external_search_error", "error": "source unavailable"}',
        name="web_search",
        tool_call_id="failed-search-1",
    )
    second = ToolMessage(
        content='{"ok": false, "error_type": "external_search_error", "error": "source unavailable again"}',
        name="web_search",
        tool_call_id="failed-search-2",
    )
    middleware = FailedToolCircuitBreakerMiddleware(["web_search"])

    # After one external failure, retries are still allowed.
    allowed = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "retry-1", "name": "web_search", "args": {"query": "retry"}},
            tool=None,
            state=SimpleNamespace(messages=[first]),
            runtime=None,
        ),
        lambda current: ToolMessage(
            content='{"ok": true}',
            name="web_search",
            tool_call_id=current.tool_call["id"],
        ),
    )
    assert isinstance(allowed, ToolMessage)
    assert allowed.status == "success"

    # After three external failures, further searches are blocked.
    third = ToolMessage(
        content='{"ok": false, "error_type": "external_search_error", "error": "still unavailable"}',
        name="web_search",
        tool_call_id="failed-search-3",
    )
    blocked = middleware.wrap_tool_call(
        ToolCallRequest(
            tool_call={"id": "retry-blocked", "name": "web_search", "args": {"query": "again"}},
            tool=None,
            state=SimpleNamespace(messages=[first, second, third]),
            runtime=None,
        ),
        lambda _request: (_ for _ in ()).throw(AssertionError("called after circuit open")),
    )
    assert isinstance(blocked, ToolMessage)
    assert blocked.status == "error"
    assert "failed 3 times" in str(blocked.content)


def test_failed_tool_circuit_counts_batch_search_failures() -> None:
    failure = '{"ok": false, "results": [{"ok": false, "error_type": "external_search_error", "error": "provider unavailable"}]}'
    messages = [
        ToolMessage(content=failure, name="web_search_batch", tool_call_id=f"batch-{index}")
        for index in range(3)
    ]
    middleware = FailedToolCircuitBreakerMiddleware(["web_search", "web_search_batch"])
    request = ToolCallRequest(
        tool_call={"id": "batch-retry", "name": "web_search_batch", "args": {"searches": []}},
        tool=None,
        state=SimpleNamespace(messages=messages),
        runtime=None,
    )

    result = middleware.wrap_tool_call(
        request,
        lambda _request: (_ for _ in ()).throw(AssertionError("batch circuit must be open")),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "failed 3 times" in str(result.content)


def test_failed_tool_circuit_allows_search_request_correction() -> None:
    invalid = ToolMessage(
        content='{"ok": false, "error_type": "invalid_search_request", "error": "step_index is out of order"}',
        name="web_search",
        tool_call_id="invalid-search",
    )
    request = ToolCallRequest(
        tool_call={"id": "corrected-search", "name": "web_search", "args": {"step_index": 2}},
        tool=None,
        state=SimpleNamespace(messages=[invalid]),
        runtime=None,
    )
    middleware = FailedToolCircuitBreakerMiddleware(["web_search"])

    result = middleware.wrap_tool_call(
        request,
        lambda current: ToolMessage(
            content='{"ok": true}',
            name="web_search",
            tool_call_id=current.tool_call["id"],
        ),
    )

    assert isinstance(result, ToolMessage)
    assert result.status == "success"


def test_evidence_manifest_failures_remain_available_to_file_validator() -> None:
    failures = [
        ToolMessage(
            content='{"ok": false, "error": "invalid coverage"}',
            name="save_evidence_manifest",
            tool_call_id=f"manifest-{index}",
        )
        for index in range(2)
    ]
    middleware = EvidenceManifestPreflightMiddleware()

    fallback = middleware.wrap_model_call(
        SimpleNamespace(messages=failures),
        lambda _request: SimpleNamespace(result=[AIMessage(content="repair manifest")]),
    )
    assert fallback.result[0].content == "repair manifest"


def test_evidence_reader_returns_explicit_empty_result_when_no_uploads(monkeypatch) -> None:
    middleware_module = import_module("knowcoder_workspace_builder.runtime.agent_tool_call_middleware")
    monkeypatch.setattr(
        middleware_module,
        "active_invocation_context",
        lambda: SimpleNamespace(stage="evidence", input={"upload_paths": []}),
    )
    request = ToolCallRequest(
        tool_call={"id": "reader-1", "name": "source_reader", "args": {"file_paths": []}},
        tool=None,
        state=SimpleNamespace(messages=[]),
        runtime=None,
    )

    result = EvidenceManifestPreflightMiddleware().wrap_tool_call(
        request,
        lambda _request: (_ for _ in ()).throw(AssertionError("reader called")),
    )

    assert isinstance(result, ToolMessage)
    payload = json.loads(str(result.content))
    assert payload == {
        "ok": True,
        "sources": [],
        "message": "No uploads were supplied. Use only the registered records under web_search.persisted, then call save_evidence_manifest.",
    }


def test_evidence_manifest_second_failure_reaches_the_persistence_tool() -> None:
    previous = ToolMessage(
        content='{"ok": false, "error": "invalid coverage"}',
        name="save_evidence_manifest",
        tool_call_id="manifest-1",
    )
    request = ToolCallRequest(
        tool_call={
            "id": "manifest-2",
            "name": "save_evidence_manifest",
            "args": {"coverage": [], "unresolved_gaps": []},
        },
        tool=None,
        state=SimpleNamespace(messages=[previous]),
        runtime=None,
    )
    middleware = EvidenceManifestPreflightMiddleware()

    called: list[str] = []

    def handler(current: ToolCallRequest) -> ToolMessage:
        called.append(current.tool_call["id"])
        return ToolMessage(
            content='{"ok": false, "error": "still invalid"}',
            name="save_evidence_manifest",
            tool_call_id=current.tool_call["id"],
        )

    result = middleware.wrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    payload = json.loads(str(result.content))
    assert payload["error"] == "still invalid"
    assert called == ["manifest-2"]


def test_evidence_manifest_rejects_missing_semantic_gap_list() -> None:
    captured: list[dict] = []
    request = ToolCallRequest(
        tool_call={
            "id": "manifest-gaps",
            "name": "save_evidence_manifest",
            "args": {"coverage": [], "unresolved_gaps": None},
        },
        tool=None,
        state=SimpleNamespace(messages=[]),
        runtime=None,
    )

    def handler(current: ToolCallRequest) -> ToolMessage:
        captured.append(current.tool_call["args"])
        return ToolMessage(content='{"ok": true}', name="save_evidence_manifest", tool_call_id="manifest-gaps")

    result = EvidenceManifestPreflightMiddleware().wrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert captured == []


def test_repeated_search_after_source_failure_returns_evidence_report() -> None:
    # Terminal failure requires the external-failure retry budget to be exhausted.
    failures = [
        ToolMessage(
            content='{"ok": false, "error_type": "external_search_error", "error": "source unavailable"}',
            name="web_search",
            tool_call_id=f"failed-{index}",
        )
        for index in range(3)
    ]
    circuit_open = ToolMessage(
        content='{"ok": false, "error_type": "tool_circuit_open", "error": "web_search failed 3 times"}',
        name="web_search",
        tool_call_id="search-circuit",
    )
    middleware = FailedToolCircuitBreakerMiddleware(["web_search"])

    response = middleware.wrap_model_call(
        SimpleNamespace(messages=[*failures, circuit_open]),
        lambda _request: (_ for _ in ()).throw(AssertionError("model called")),
    )

    message = str(response.result[0].content)
    assert "source service failed" in message.casefold()
    assert "3" in message
