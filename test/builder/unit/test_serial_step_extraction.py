"""Chunk extraction planning, concurrency, and deterministic draft merging."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.runtime.candidate_normalization import (
    merge_schema_blueprint,
    normalize_instance_batch,
)
from knowcoder_workspace_builder.service.stage_runner import StageRunner
from knowcoder_workspace_builder.service.coordinator import (
    _affected_extraction_step_indexes,
    _extraction_units_for_sources,
    _sources_for_step_indexes,
)
from knowcoder_workspace_builder.storage.stage_artifacts import merge_draft, merge_final_drafts
from knowcoder_workspace_builder.tools import unstructured_extractor
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import Stage


def test_extraction_units_create_one_task_per_selected_chunk() -> None:
    evidence = {
        "coverage": [
            {
                "step": "Collect paper A facts",
                "status": "covered",
                "requirements": ["title"],
                "source_ids": ["src-a", "src-shared"],
                "chunk_refs": [
                    {"source_id": "src-a", "chunk_id": "src-a#chunk-1"},
                    {"source_id": "src-a", "chunk_id": "src-a#chunk-2"},
                    {"source_id": "src-shared", "chunk_id": "src-shared#chunk-1"},
                ],
            },
            {
                "step": "Collect paper B facts",
                "status": "covered",
                "requirements": ["title"],
                "source_ids": ["src-b", "src-shared"],
                "chunk_refs": [
                    {"source_id": "src-b", "chunk_id": "src-b#chunk-1"},
                    {"source_id": "src-shared", "chunk_id": "src-shared#chunk-2"},
                ],
            },
        ]
    }
    assigned = [
        {"source_id": "src-a", "file_path": "a.md"},
        {"source_id": "src-b", "file_path": "b.md"},
        {"source_id": "src-shared", "file_path": "s.md"},
        {"source_id": "src-extra", "file_path": "e.md"},
    ]
    units = _extraction_units_for_sources(evidence, assigned)
    assert [unit["step_index"] for unit in units] == [1, 1, 1, 2, 2, 3]
    assert [unit["unit_index"] for unit in units] == [1, 2, 3, 4, 5, 6]
    assert all(len(unit["source_ids"]) == 1 for unit in units)
    assert all(len(unit["chunk_refs"]) == 1 for unit in units[:-1])
    assert units[-1]["source_ids"] == ["src-extra"]
    assert units[-1]["chunk_refs"] == []


def test_extract_live_activity_keeps_the_real_unit_index() -> None:
    state = BuildState(session_id="session-live-unit-1234", question="Extract facts", upload_paths=[])
    event = {
        "type": "activity",
        "run_index": 17,
        "extract_unit_index": 17,
        "message": {
            "kind": "tool",
            "run_index": 17,
            "extract_unit_index": 17,
        },
    }

    payload = StageRunner._live_event(
        event,
        state,
        Stage.EXTRACT,
        "attempt-parent",
        1,
        "turn-1",
        "invocation-1",
    )

    assert payload["run_index"] == 17
    assert payload["message"]["run_index"] == 17


def test_extension_extraction_targets_only_added_steps() -> None:
    baseline_step = "Collect release facts."
    added_step = "Collect free-threading facts."
    state = BuildState(session_id="session-extension-1234", question="Extend the research", upload_paths=[])
    state.workspace_mode = "extend"
    state.extension_baseline_steps = [baseline_step]
    steps = [baseline_step, added_step]
    evidence = {
        "coverage": [
            {"step_index": 1, "step": baseline_step, "source_ids": ["src-old", "src-shared"]},
            {
                "step_index": 2,
                "step": added_step,
                "source_ids": ["src-new", "src-shared"],
                "chunk_refs": [
                    {"source_id": "src-new", "chunk_id": "src-new#chunk-1"},
                    {"source_id": "src-shared", "chunk_id": "src-shared#chunk-1"},
                ],
            },
        ]
    }
    assigned = [
        {"source_id": "src-old", "file_path": "old.md"},
        {"source_id": "src-new", "file_path": "new.md"},
        {"source_id": "src-shared", "file_path": "shared.md"},
    ]

    indexes = _affected_extraction_step_indexes(state, steps)
    selected = _sources_for_step_indexes(evidence, assigned, indexes or [])
    units = _extraction_units_for_sources(evidence, selected, indexes)

    assert indexes == [2]
    assert [source["source_id"] for source in selected] == ["src-new", "src-shared"]
    assert [unit["step_index"] for unit in units] == [2, 2]
    assert [unit["source_ids"] for unit in units] == [["src-new"], ["src-shared"]]
    assert [unit["unit_index"] for unit in units] == [1, 2]


def test_evidence_acceptance_carries_affected_steps_into_extraction() -> None:
    state = BuildState(session_id="session-evidence-1234", question="Refresh evidence", upload_paths=[])
    state.stage = Stage.EVIDENCE
    state.active_attempt_id = "attempt-evidence"
    result = StageResult(
        ok=True,
        stage=str(Stage.EVIDENCE),
        status="completed",
        report="Evidence ready.",
        handoff={"coverage": [], "sources": [], "unresolved_gaps": []},
        artifacts={},
        errors=(),
    )

    accepted = StageRunner._accept(
        state,
        "attempt-evidence",
        result,
        evidence_step_indexes=[2],
    )

    assert accepted.pending_evidence_step_indexes == [2]
    assert accepted.stage == Stage.SCHEMA_BUILD


class RateLimitError(RuntimeError):
    pass


def test_chunk_extraction_keeps_concurrency_after_rate_limit_and_retries_only_failed_chunks(monkeypatch) -> None:
    calls: dict[str, int] = {}
    progress: list[tuple[int, int, str, int]] = []
    lock = threading.Lock()

    def fake_extract(_client, _model, _schema_outline, chunk):
        source_id = str(chunk["source_id"])
        with lock:
            calls[source_id] = calls.get(source_id, 0) + 1
            call_number = calls[source_id]
        if source_id == "src-2" and call_number == 1:
            raise RateLimitError("rate limited")
        return {"entities": [], "relations": []}

    monkeypatch.setattr(unstructured_extractor, "_extract_one", fake_extract)
    monkeypatch.setenv("SCHEMA_EXTRACT_PARALLEL_WORKERS", "3")
    monkeypatch.setenv("SCHEMA_EXTRACT_FALLBACK_WORKERS", "2")
    monkeypatch.setenv("SCHEMA_EXTRACT_CHUNK_RETRY_LIMIT", "2")
    chunks = [{"source_id": f"src-{index}"} for index in range(1, 5)]

    results, retries, final_workers, fallback_triggered = unstructured_extractor._run_chunk_requests(
        object(),
        "gpt-4o-mini",
        {},
        chunks,
        on_progress=lambda index, total, status, completed: progress.append((index, total, status, completed)),
    )

    assert set(results) == {0, 1, 2, 3}
    assert calls == {"src-1": 1, "src-2": 2, "src-3": 1, "src-4": 1}
    assert retries == {1: 1}
    assert final_workers == 3
    assert fallback_triggered is False
    assert progress[0:3] == [(1, 4, "running", 0), (2, 4, "running", 0), (3, 4, "running", 0)]
    assert sum(1 for item in progress if item[:3] == (2, 4, "running")) == 2
    completed_counts = [item[3] for item in progress if item[2] == "done"]
    assert completed_counts == sorted(completed_counts)
    assert completed_counts == [1, 2, 3, 4]


def test_chunk_extraction_reports_each_completion_without_waiting_for_worker_wave(monkeypatch) -> None:
    release_later_chunks = threading.Event()
    first_chunk_reported = threading.Event()
    progress: list[tuple[int, int, str, int]] = []
    outcome: dict[str, object] = {}

    def fake_extract(_client, _model, _schema_outline, chunk):
        if chunk["source_id"] != "src-1":
            assert release_later_chunks.wait(timeout=3)
        return {"entities": [], "relations": []}

    def on_progress(index: int, total: int, status: str, completed: int) -> None:
        progress.append((index, total, status, completed))
        if index == 1 and status == "done":
            first_chunk_reported.set()

    def run_extraction() -> None:
        outcome["value"] = unstructured_extractor._run_chunk_requests(
            object(),
            "gpt-4o-mini",
            {},
            [{"source_id": f"src-{index}"} for index in range(1, 4)],
            on_progress=on_progress,
        )

    monkeypatch.setattr(unstructured_extractor, "_extract_one", fake_extract)
    monkeypatch.setenv("SCHEMA_EXTRACT_PARALLEL_WORKERS", "3")
    worker = threading.Thread(target=run_extraction)
    worker.start()
    try:
        assert first_chunk_reported.wait(timeout=1)
        assert [item for item in progress if item[2] == "done"] == [(1, 3, "done", 1)]
    finally:
        release_later_chunks.set()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert "value" in outcome


def test_merge_draft_dedupes_same_type_name_with_different_ids() -> None:
    first = merge_draft(
        {"processed_source_ids": [], "entities": [], "relations": []},
        {
            "entities": [
                {
                    "type": "Paper",
                    "id": "paper-a-1",
                    "name": "Demo Paper",
                    "attributes": {"year": 2024},
                    "source_refs": ["src-1"],
                }
            ],
            "relations": [],
        },
    )
    second = merge_draft(
        first,
        {
            "entities": [
                {
                    "type": "Paper",
                    "id": "paper-a-2",
                    "name": "Demo Paper",
                    "attributes": {"venue": "ACL"},
                    "source_refs": ["src-2"],
                }
            ],
            "relations": [
                {
                    "type": "about",
                    "head": {"type": "Paper", "id": "paper-a-2"},
                    "tail": {"type": "Topic", "id": "topic-1"},
                    "attributes": {},
                    "source_refs": ["src-2"],
                }
            ],
        },
    )
    assert len(second["entities"]) == 1
    paper = second["entities"][0]
    assert paper["id"] == "paper-a-1"
    assert paper["attributes"] == {"year": 2024, "venue": "ACL"}
    assert paper["source_refs"] == ["src-1", "src-2"]
    assert second["relations"][0]["head"]["id"] == "paper-a-1"


def test_merge_draft_separates_different_names_that_reuse_one_model_id() -> None:
    first = merge_draft(
        {"processed_source_ids": [], "entities": [], "relations": []},
        {
            "entities": [
                {
                    "type": "Paper",
                    "id": "paper_1",
                    "name": "First Paper",
                    "attributes": {"year": 2024},
                    "source_refs": ["src-1"],
                }
            ],
            "relations": [],
        },
    )
    second = merge_draft(
        first,
        {
            "entities": [
                {
                    "type": "Paper",
                    "id": "paper_1",
                    "name": "Second Paper",
                    "attributes": {"year": 2025},
                    "source_refs": ["src-2"],
                }
            ],
            "relations": [
                {
                    "type": "references",
                    "head": {"type": "Paper", "id": "paper_1"},
                    "tail": {"type": "Paper", "id": "paper_1"},
                    "attributes": {},
                    "source_refs": ["src-2"],
                }
            ],
        },
    )

    assert [entity["name"] for entity in second["entities"]] == ["First Paper", "Second Paper"]
    first_id, second_id = [entity["id"] for entity in second["entities"]]
    assert first_id == "paper_1"
    assert second_id != first_id
    assert second["relations"][0]["head"]["id"] == second_id
    assert second["relations"][0]["tail"]["id"] == second_id

    third = merge_draft(
        second,
        {
            "entities": [
                {
                    "type": "Paper",
                    "id": "another-temporary-id",
                    "name": "Second Paper",
                    "attributes": {"venue": "DemoConf"},
                    "source_refs": ["src-3"],
                }
            ],
            "relations": [],
        },
    )
    assert len(third["entities"]) == 2
    assert third["entities"][1]["id"] == second_id
    assert third["entities"][1]["attributes"] == {"year": 2025, "venue": "DemoConf"}


def test_unstructured_extractor_audits_only_relations_with_missing_endpoints() -> None:
    entity = {
        "type": "Paper",
        "id": "paper-1",
        "name": "Demo Paper",
        "attributes": {},
        "source_refs": ["src-1"],
    }
    valid_relation = {
        "type": "cites",
        "head": {"type": "Paper", "id": "paper-1"},
        "tail": {"type": "Paper", "id": "paper-1"},
        "attributes": {},
        "source_refs": ["src-1"],
    }
    invalid_relation = {
        "type": "cites",
        "head": {"type": "Paper", "id": "paper-1"},
        "tail": {"type": "Paper", "id": "missing-paper"},
        "attributes": {},
        "source_refs": ["src-1"],
    }

    cleaned, rejected = unstructured_extractor._remove_relations_with_missing_endpoints(
        {"entities": [entity], "relations": [valid_relation, invalid_relation]}
    )

    assert cleaned["entities"] == [entity]
    assert cleaned["relations"] == [valid_relation]
    assert rejected == [
        {
            "position": 2,
            "reason": "relation_endpoint_missing",
            "missing_endpoints": [
                {"endpoint": "tail", "type": "Paper", "id": "missing-paper"}
            ],
            "relation": invalid_relation,
        }
    ]


def test_instance_normalization_moves_extra_fact_fields_into_attributes() -> None:
    entities, relations, changes = normalize_instance_batch(
        entities=[
            {
                "type": "RuntimeFeature",
                "id": "feature-jit",
                "name": "Experimental JIT",
                "attributes": ["experimental", "disabled by default"],
                "introduced_in": "3.13",
            }
        ],
        relations=[
            {
                "type": "available_in",
                "head": {"type": "RuntimeFeature", "id": "feature-jit"},
                "tail": {"type": "PythonVersion", "id": "python-3.13"},
                "attributes": "official",
                "confidence": "official",
            }
        ],
        source_ids=["src-1"],
        evidence_refs=[{"source_id": "src-1", "chunk_id": "src-1#chunk-1"}],
    )

    assert entities[0]["attributes"] == {
        "value": ["experimental", "disabled by default"],
        "introduced_in": "3.13",
    }
    assert relations[0]["attributes"] == {"value": "official", "confidence": "official"}
    assert [change["action"] for change in changes].count("moved") == 2
    assert [change["action"] for change in changes].count("wrapped") == 2


def test_instance_normalization_fills_empty_attributes_and_ignores_endpoint_notes() -> None:
    entities, relations, changes = normalize_instance_batch(
        entities=[{"type": "Paper", "id": "paper-1", "name": "Demo"}],
        relations=[
            {
                "type": "cites",
                "head": {"type": "Paper", "id": "paper-1", "note": "same chunk"},
                "tail": {"type": "Paper", "id": "paper-1"},
            }
        ],
        source_ids=["src-1"],
    )

    assert entities[0]["attributes"] == {}
    assert relations[0]["attributes"] == {}
    assert relations[0]["head"] == {"type": "Paper", "id": "paper-1"}
    assert {change["action"] for change in changes} >= {"derived", "ignored"}


def test_extraction_json_ignores_non_contract_top_level_fields() -> None:
    batch, changes = unstructured_extractor._json_object(
        json.dumps({"entities": [], "relations": [], "summary": "No relevant facts."})
    )

    assert batch == {"entities": [], "relations": []}
    assert changes == [
        {
            "field": "summary",
            "action": "ignored",
            "detail": "Ignored a field outside the canonical extraction result contract.",
        }
    ]


def test_schema_normalization_ignores_notes_and_deduplicates_identical_definitions() -> None:
    entity = {
        "name": "Paper",
        "id_type": "str",
        "description": "A paper.",
        "attributes": [
            {"name": "year", "type": "int", "optional": True, "note": "Publication year."}
        ],
        "reasoning": "Needed by the research question.",
    }

    blueprint, changes = merge_schema_blueprint(
        {"entities": [], "relations": []},
        entities=[entity, dict(entity)],
        relations=[],
        remove_entity_names=[],
        remove_relation_names=[],
    )

    assert [item["name"] for item in blueprint["entities"]] == ["Paper"]
    assert {change["action"] for change in changes} >= {"deduplicated", "ignored"}


def test_chunk_extraction_repairs_invalid_model_contract(monkeypatch) -> None:
    responses = iter(
        [
            {"entities": [{"type": "Paper", "id": "paper-1", "attributes": {}}], "relations": []},
            {
                "entities": [
                    {
                        "type": "Paper",
                        "id": "paper-1",
                        "name": "Demo",
                        "attributes": {},
                    }
                ],
                "relations": [],
            },
        ]
    )
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        content = json.dumps(next(responses))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("SCHEMA_EXTRACT_FORMAT_REPAIR_LIMIT", "2")
    result = unstructured_extractor._extract_one(
        client,
        "gpt-4o-mini",
        {"entities": []},
        {
            "step_index": 1,
            "step": "Collect paper facts",
            "requirements": ["title"],
            "source_id": "src-1",
            "chunk_id": "src-1#chunk-1",
            "title": "Source",
            "url": "https://example.test/source",
            "text": "Demo is a paper.",
        },
    )

    assert result["format_repair_count"] == 1
    assert result["entities"][0]["attributes"] == {}
    assert len(calls) == 2
    assert "name must be non-empty text" in calls[1]["messages"][-1]["content"]
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload == {
        "schema_outline": {"entities": []},
        "content": "Demo is a paper.",
    }


def test_merge_draft_reconciles_compatible_type_aliases() -> None:
    first = merge_draft(
        {"processed_source_ids": [], "entities": [], "relations": []},
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-austin-1",
                    "name": "Austin MSA",
                    "attributes": {"type": "MSA"},
                    "source_refs": ["src-1"],
                }
            ],
            "relations": [],
        },
    )
    second = merge_draft(
        first,
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-austin-2",
                    "name": "Austin MSA",
                    "attributes": {"type": "Metropolitan Statistical Area"},
                    "source_refs": ["src-2"],
                }
            ],
            "relations": [],
        },
    )
    assert len(second["entities"]) == 1
    entity = second["entities"][0]
    assert entity["id"] == "geo-austin-1"
    assert entity["attributes"]["type"] in {
        "MSA",
        "Metropolitan Statistical Area",
    }
    assert set(entity["source_refs"]) == {"src-1", "src-2"}


def test_merge_draft_keeps_city_and_msa_separate() -> None:
    first = merge_draft(
        {"processed_source_ids": [], "entities": [], "relations": []},
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-msa",
                    "name": "Austin MSA",
                    "attributes": {"type": "MSA"},
                    "source_refs": ["src-1"],
                }
            ],
            "relations": [],
        },
    )
    second = merge_draft(
        first,
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-city",
                    "name": "Austin, TX",
                    "attributes": {"type": "City"},
                    "source_refs": ["src-2"],
                }
            ],
            "relations": [],
        },
    )
    assert len(second["entities"]) == 2


def test_merge_draft_keeps_first_value_on_hard_attribute_conflict() -> None:
    first = merge_draft(
        {"processed_source_ids": [], "entities": [], "relations": []},
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-1",
                    "name": "Austin",
                    "attributes": {"type": "City"},
                    "source_refs": ["src-1"],
                }
            ],
            "relations": [],
        },
    )
    second = merge_draft(
        first,
        {
            "entities": [
                {
                    "type": "Geography",
                    "id": "geo-1",
                    "name": "Austin",
                    "attributes": {"type": "County"},
                    "source_refs": ["src-2"],
                }
            ],
            "relations": [],
        },
    )
    assert second["entities"][0]["attributes"]["type"] == "City"
    assert set(second["entities"][0]["source_refs"]) == {"src-1", "src-2"}


def test_final_merge_prefers_later_authoritative_stage_values() -> None:
    web_draft = {
        "entities": [
            {
                "type": "Paper",
                "id": "paper-web",
                "name": "Deep learning",
                "attributes": {"title": "Deep learning", "citation_count": 22, "year": 2015},
                "source_refs": ["crawl-source"],
            }
        ],
        "relations": [],
    }
    structured_draft = {
        "entities": [
            {
                "type": "Paper",
                "id": "paper-upload",
                "name": "Deep Learning",
                "attributes": {"title": "Deep Learning", "citation_count": 93450, "year": 2015},
                "source_refs": ["upload-source"],
            }
        ],
        "relations": [],
    }

    merged = merge_final_drafts([web_draft, structured_draft])

    assert len(merged["entities"]) == 1
    paper = merged["entities"][0]
    assert paper["name"] == "Deep Learning"
    assert paper["attributes"]["title"] == "Deep Learning"
    assert paper["attributes"]["citation_count"] == 93450
    assert set(paper["source_refs"]) == {"crawl-source", "upload-source"}
