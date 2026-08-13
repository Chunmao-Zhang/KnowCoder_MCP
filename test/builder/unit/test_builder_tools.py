from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from typing import Any, Iterator

from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.session_context import harness_session_environment
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for, virtual_session_path
from knowcoder_workspace_builder.runtime.workspace_sources import register_source_record
from knowcoder_workspace_builder.service.finalize import _complete_referenced_sources, finalize_workspace
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout, SessionPaths
from knowcoder_workspace_builder.storage.tool_calls import SearchLedger
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.sources import SourceRepository
from knowcoder_workspace_builder.tools.stage_artifacts import (
    append_instances_batch,
    append_instances_batches_from_file,
    save_evidence_manifest,
    save_schema,
    save_schema_judgement,
)
from knowcoder_workspace_builder.tools.schema_validator import schema_validator
from knowcoder_workspace_builder.tools.source_reader import MAX_UNSTRUCTURED_SOURCE_BATCH, source_reader
from knowcoder_workspace_builder.workflow.sources import split_sources


SCHEMA_SOURCE = """from typing import List


class Entity:
    name: str


class Person(Entity):
    \"\"\"A person named by the source.\"\"\"
    _id: str
    name: str
    employers: List[\"Company\"]
    \"\"\"Links the person to their employing companies.\"\"\"


class Company(Entity):
    \"\"\"An employing organization.\"\"\"
    _id: str
    name: str
"""


def test_source_repository_preserves_concurrent_registrations(runtime_project: Path) -> None:
    paths = ProjectLayout(runtime_project).session("session-concurrent-sources", create=True)
    records = [
        {
            "source_id": f"source-{index}",
            "source_kind": "web_crawl",
            "title": f"Source {index}",
        }
        for index in range(12)
    ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        saved = list(
            pool.map(
                lambda record: SourceRepository(paths).register("web_crawls", record),
                records,
            )
        )

    assert len(saved) == len(records)
    assert {item["source_id"] for item in SourceRepository(paths).list()} == {
        item["source_id"] for item in records
    }


WORKSPACE_README = """---
name: "People and Employers"
description: "A reusable Workspace for employment relationships."
finish:
  completed: true
  details: "Completed Schema construction and instance extraction."
---

# Workspace Overview

The Workspace contains validated employment records.

## Completed Research

- Identify every person and employer relationship.

## Schema and Data

- The Schema and instances are complete.

## Main Files

- `ontology/types.py`
- `data/entities.jsonl`
- `data/relations.jsonl`

## Sources

- Sources are listed in `data/manifest.json`.

## Incremental Extension

Extend the existing files for newly confirmed requirements.
"""


def test_finalize_restores_referenced_baseline_source_metadata(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-baseline-source-merge", create=True)
    baseline_source = {
        "source_id": "source-baseline",
        "source_kind": "web_crawl",
        "file_path": "/.knowcoder_workspace/intermediate/sources/web_crawls/source-baseline/content.md",
        "title": "Baseline source",
    }
    SourceRepository(paths).register("web_crawls", baseline_source)
    instances = {
        "entities": [{"source_refs": ["source-current", "source-baseline"]}],
        "relations": [],
    }

    merged = _complete_referenced_sources(
        paths,
        [{"source_id": "source-current", "title": "Current source"}],
        instances,
    )

    assert [source["source_id"] for source in merged] == ["source-current", "source-baseline"]


def test_web_search_bundles_are_unstructured_while_uploaded_json_is_structured() -> None:
    web_bundle = {
        "source_id": "web-1",
        "source_kind": "web_search_bundle",
        "file_path": "/.knowcoder_workspace/intermediate/sources/web_search/search.json",
    }
    uploaded_json = {
        "source_id": "upload-1",
        "source_kind": "upload",
        "file_path": "/.knowcoder_workspace/intermediate/sources/user_uploads/records.json",
    }

    split = split_sources([web_bundle, uploaded_json])

    assert split == {"unstructured": [web_bundle], "structured": [uploaded_json]}


def test_source_reader_returns_web_search_bundles_as_text_chunks(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-web-bundle-1234", create=True)
    source_path = paths.sources / "web_search" / "search.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        json.dumps(
            {
                "query": "official filing revenue",
                "results": [
                    {
                        "title": "Official filing",
                        "link": "https://example.test/filing",
                        "snippet": "Revenue was 100.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_path_value = virtual_session_path(source_path.relative_to(paths.root).as_posix())
    stage_input = {
        "schema_outline": {"entities": []},
        "sources": [],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }

    with _active_attempt(layout, paths, "extract", stage_input):
        source = register_source_record(
            paths.root,
            "web_search",
            {
                "source_id": "search-bundle-1",
                "source_kind": "web_search_bundle",
                "file_path": source_path_value,
                "file_type": "json",
                "title": "Official filing search",
            },
        )
        result = _decoded(source_reader.invoke({"file_paths": [source["file_path"]]}))

    assert result["ok"] is True
    assert result["sources"][0]["sample_rows"] == []
    assert result["sources"][0]["chunks"]
    assert "Revenue was 100" in result["sources"][0]["chunks"][0]["text"]


def test_web_crawl_reader_and_extractor_preserve_selected_chunk_reference(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-web-chunk-extract-1234", create=True)
    source_id = "crawl-complete-source"
    directory = paths.sources / "web_crawls" / source_id
    content_path = directory / "content.md"
    chunk_path = directory / "chunks.jsonl"
    content_path.parent.mkdir(parents=True)
    content_path.write_text("# Report\n\nUnrelated context.\n\nRevenue increased in 2025.\n", encoding="utf-8")
    chunks = [
        {
            "source_id": source_id,
            "chunk_id": f"{source_id}#chunk_0001",
            "heading": "Report",
            "start": 0,
            "end": 28,
            "text": "Unrelated context.",
            "content_sha256": "hash-1",
        },
        {
            "source_id": source_id,
            "chunk_id": f"{source_id}#chunk_0002",
            "heading": "Report",
            "start": 29,
            "end": 55,
            "text": "Revenue increased in 2025.",
            "content_sha256": "hash-2",
        },
    ]
    chunk_path.write_text("".join(json.dumps(item) + "\n" for item in chunks), encoding="utf-8")
    source = SourceRepository(paths).register(
        "web_crawls",
        {
            "source_id": source_id,
            "source_kind": "web_crawl",
            "file_path": virtual_session_path(content_path.relative_to(paths.root).as_posix()),
            "chunk_path": virtual_session_path(chunk_path.relative_to(paths.root).as_posix()),
            "file_type": "md",
            "title": "Complete report",
            "content_sha256": "source-hash",
            "chunk_count": 2,
        },
    )
    selected_ref = {"source_id": source_id, "chunk_id": f"{source_id}#chunk_0002"}
    stage_input = {
        "schema_outline": parse_schema(SCHEMA_SOURCE).outline(),
        "sources": [source],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {
            "extraction_units": [
                {
                    "step_index": 1,
                    "step": "Collect revenue evidence.",
                    "requirements": ["2025 revenue"],
                    "source_ids": [source_id],
                    "chunk_refs": [selected_ref],
                }
            ],
            "current_step_index": 1,
        },
    }
    with _active_attempt(layout, paths, "extract", stage_input) as attempt:
        read_result = _decoded(source_reader.invoke({"file_paths": ["*"]}))
        append_result = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [
                        {
                            "type": "Company",
                            "id": "company-1",
                            "name": "Example Company",
                            "attributes": {"revenue_change": "increased in 2025"},
                        }
                    ],
                    "relations": [],
                }
            )
        )
        draft = json.loads(
            (paths.attempts / attempt["attempt_id"] / "unstructured_draft.json").read_text(encoding="utf-8")
        )

    assert read_result["ok"] is True
    assert [item["chunk_id"] for item in read_result["sources"][0]["chunks"]] == [selected_ref["chunk_id"]]
    assert append_result["ok"] is True
    assert draft["entities"][0]["source_refs"] == [source_id]
    assert draft["entities"][0]["evidence_refs"] == [selected_ref]


def test_unstructured_source_reader_requires_small_batches(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-small-source-batch-1234", create=True)
    stage_input = {
        "schema_outline": {"entities": []},
        "sources": [],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }
    oversized = [
        f"/.knowcoder_workspace/intermediate/sources/source-{index}.txt"
        for index in range(MAX_UNSTRUCTURED_SOURCE_BATCH + 1)
    ]

    with _active_attempt(layout, paths, "extract", stage_input):
        result = _decoded(source_reader.invoke({"file_paths": oversized}))

    assert result["ok"] is False
    assert result["error_type"] == "source_batch_too_large"
    assert result["max_batch_sources"] == MAX_UNSTRUCTURED_SOURCE_BATCH


def test_unstructured_extractor_reads_all_assigned_sources_and_appends_one_complete_batch(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-consolidated-extract-1234", create=True)
    records = []
    for source_id, text in (
        ("source-person", "Ada is a person."),
        ("source-company", "Ada works for Analytical Engines Ltd."),
    ):
        source_path = paths.sources / f"{source_id}.md"
        source_path.write_text(text, encoding="utf-8")
        records.append(
            SourceRepository(paths).register(
                "web_search",
                {
                    "source_id": source_id,
                    "source_kind": "web",
                    "file_path": virtual_session_path(source_path.relative_to(paths.root).as_posix()),
                    "file_type": "md",
                    "title": source_id,
                },
            )
        )
    stage_input = {
        "schema_outline": parse_schema(SCHEMA_SOURCE).outline(),
        "sources": records,
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }

    with _active_attempt(layout, paths, "extract", stage_input):
        read_result = _decoded(source_reader.invoke({"file_paths": ["*"]}))
        append_result = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [
                        {
                            "type": "Person",
                            "id": "person-ada",
                            "name": "Ada",
                            "attributes": {},
                        },
                        {
                            "type": "Company",
                            "id": "company-analytical-engines",
                            "name": "Analytical Engines Ltd",
                            "attributes": {},
                        },
                    ],
                    "relations": [
                        {
                            "type": "employers",
                            "head": {"type": "Person", "id": "person-ada"},
                            "tail": {"type": "Company", "id": "company-analytical-engines"},
                            "attributes": {},
                        }
                    ],
                }
            )
        )

    assert read_result["ok"] is True
    assert [item["source_id"] for item in read_result["sources"]] == ["source-person", "source-company"]
    assert append_result["ok"] is True
    assert append_result["processed_source_ids"] == ["source-person", "source-company"]
    assert append_result["entity_count"] == 2
    assert append_result["relation_count"] == 1


def test_unstructured_append_derives_runtime_source_coverage(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-consolidated-boundary-1234", create=True)
    records = []
    for source_id in ("source-person", "source-company"):
        source_path = paths.sources / f"{source_id}.md"
        source_path.write_text(f"Content from {source_id}.", encoding="utf-8")
        records.append(
            SourceRepository(paths).register(
                "web_search",
                {
                    "source_id": source_id,
                    "source_kind": "web",
                    "file_path": virtual_session_path(source_path.relative_to(paths.root).as_posix()),
                    "file_type": "md",
                    "title": source_id,
                },
            )
        )
    stage_input = {
        "schema_outline": parse_schema(SCHEMA_SOURCE).outline(),
        "sources": records,
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }

    with _active_attempt(layout, paths, "extract", stage_input):
        result = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [],
                    "relations": [],
                }
            )
        )

    assert result["ok"] is True
    assert result["processed_source_ids"] == ["source-person", "source-company"]
    assert result["extraction_complete"] is True


def test_unstructured_append_accepts_equivalent_numeric_source_format(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-numeric-source-1234", create=True)
    schema_source = '''class Entity:
    name: str


class Team(Entity):
    """A team with a sourced market value."""
    _id: str
    name: str
    market_value: float
'''
    source_path = paths.sources / "market-value.md"
    source_path.write_text("The squad market value is \u20ac807.50 million.", encoding="utf-8")
    source = SourceRepository(paths).register(
        "web_search",
        {
            "source_id": "source-market-value",
            "source_kind": "web",
            "file_path": virtual_session_path(source_path.relative_to(paths.root).as_posix()),
            "file_type": "md",
            "title": "Market value source",
        },
    )
    stage_input = {
        "schema_outline": parse_schema(schema_source, require_relations=False).outline(),
        "sources": [source],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }

    with _active_attempt(layout, paths, "extract", stage_input):
        result = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [
                        {
                            "type": "Team",
                            "id": "team-argentina",
                            "name": "Argentina",
                            "attributes": {"market_value": 807.5},
                        }
                    ],
                    "relations": [],
                }
            )
        )

    assert result["ok"] is True
    assert result["entity_count"] == 1
    assert result["processed_source_ids"] == ["source-market-value"]


def test_structured_append_treats_csv_delimiters_as_cell_boundaries(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-csv-numeric-1234", create=True)
    schema_source = '''class Entity:
    name: str


class TeamResult(Entity):
    """A team result copied from one structured row."""
    _id: str
    name: str
    matches_played: int
    wins: int
    draws: int
'''
    source_path = paths.sources / "team-results.csv"
    source_path.write_text("team,matches_played,wins,draws\nArgentina,7,4,2\n", encoding="utf-8")
    source = SourceRepository(paths).register(
        "user_uploads",
        {
            "source_id": "upload-team-results",
            "source_kind": "upload",
            "file_path": virtual_session_path(source_path.relative_to(paths.root).as_posix()),
            "file_type": "csv",
            "title": "Team results",
        },
    )
    stage_input = {
        "schema_outline": parse_schema(schema_source, require_relations=False).outline(),
        "sources": [source],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/structured_draft.json",
        "work_dir": "/.knowcoder_workspace/intermediate/sources",
        "workspace_context": {},
    }

    with _active_attempt(layout, paths, "structured_extract", stage_input) as attempt:
        batch_path = paths.attempts / str(attempt["attempt_id"]) / "structured_batches.json"
        batch_path.write_text(
            json.dumps(
                {
                    "generation_note": "Rows were extracted from the uploaded table.",
                    "batches": [
                        {
                            "batch_note": "First data row.",
                            "entities": [
                                {
                                    "type": "TeamResult",
                                    "id": "team-result-argentina",
                                    "name": "Argentina",
                                    "attributes": {"matches_played": 7, "wins": 4, "draws": 2},
                                }
                            ],
                            "relations": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        result = _decoded(append_instances_batches_from_file.invoke({}))

    assert result["ok"] is True
    assert result["entity_count"] == 1
    assert result["processed_source_ids"] == ["upload-team-results"]


@contextmanager
def _active_attempt(
    layout: ProjectLayout,
    paths: SessionPaths,
    stage: str,
    stage_input: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    store = AttemptStore(layout)
    number = 1
    attempt = store.start(paths.session_id, stage, number)
    if stage == "structured_extract" and "batch_path" not in stage_input:
        stage_input = {
            **stage_input,
            "batch_path": virtual_session_path(
                f"intermediate/attempts/{attempt['attempt_id']}/structured_batches.json"
            ),
        }
    write_invocation_context(paths, attempt["attempt_id"], stage, stage_input)
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


def _decoded(tool_result: str) -> dict[str, Any]:
    value = json.loads(tool_result)
    assert isinstance(value, dict)
    return value


def test_schema_validator_returns_schema_defects_as_review_findings() -> None:
    result = _decoded(
        schema_validator.invoke(
            {
                "schema_source": "class Entity:\n    _id: str\n    name: str\n\nclass Record(Entity):\n    _id: str\n    name: str\n",
            }
        )
    )

    assert result["ok"] is True
    assert result["valid"] is False
    assert result["findings"]
    assert result["errors"] == []


def test_stage_tools_build_one_verified_four_file_workspace(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-tools-1234", create=True)
    source_path = paths.sources / "user_uploads" / "people.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("person,company\nAda,Analytical Engines Ltd\n", encoding="utf-8")

    evidence_input = {
        "question": "List people and their employers.",
        "steps": ["Identify every person and employer relationship."],
        "upload_paths": [virtual_session_path("intermediate/sources/user_uploads/people.csv")],
        "research_dir": "/.knowcoder_workspace/intermediate",
        "workspace_context": {"required_source_ids": ["upload-people"]},
    }
    with _active_attempt(layout, paths, "evidence", evidence_input) as evidence_attempt:
        source_virtual_path = virtual_path_for(paths.root, source_path)
        source = register_source_record(
            paths.root,
            "user_uploads",
            {
                "source_id": "upload-people",
                "source_kind": "upload",
                "file_path": source_virtual_path,
                "file_type": "csv",
                "title": "People",
            },
        )
        evidence_result = _decoded(
            save_evidence_manifest.invoke(
                {
                    "coverage": [
                        {
                            "step_index": 1,
                            "status": "covered",
                        }
                    ],
                    "unresolved_gaps": [],
                }
            )
        )
        assert evidence_result["ok"] is True
        evidence_manifest = json.loads(
            (paths.attempts / evidence_attempt["attempt_id"] / "evidence_manifest.json").read_text(encoding="utf-8")
        )
        assert evidence_manifest["coverage"][0]["step"] == evidence_input["steps"][0]
    schema_input = {
        "question": evidence_input["question"],
        "steps": evidence_input["steps"],
        "data_manifest": evidence_manifest,
        "workspace_context": {},
    }
    with _active_attempt(layout, paths, "schema_build", schema_input) as schema_attempt:
        schema_tool_result = _decoded(
            save_schema.invoke(
                {
                    "entities": [
                        {
                            "name": "Person",
                            "id_type": "str",
                            "description": "A person named by the source.",
                            "attributes": [],
                        },
                        {
                            "name": "Company",
                            "id_type": "str",
                            "description": "An employing organization.",
                            "attributes": [],
                        },
                    ],
                    "relations": [
                        {
                            "name": "employers",
                            "head": "Person",
                            "tail": "Company",
                            "description": "Links the person to their employing companies.",
                            "many": True,
                            "optional": False,
                        }
                    ],
                }
            )
        )
        assert schema_tool_result["ok"] is True
        schema_draft = paths.attempts / schema_attempt["attempt_id"] / "schema_draft.py"
        schema_outline = parse_schema(schema_draft.read_text(encoding="utf-8")).outline()

    judge_input = {
        "question": evidence_input["question"],
        "steps": evidence_input["steps"],
        "data_manifest": evidence_manifest,
        "schema_source": SCHEMA_SOURCE,
        "workspace_context": {"mode": "standard"},
    }
    with _active_attempt(layout, paths, "schema_judge", judge_input) as judge_attempt:
        judgement = _decoded(
            save_schema_judgement.invoke(
                {
                    "decision": " PASS ",
                    "missing_requirements": ["This note contradicts the pass decision."],
                }
            )
        )
        assert judgement["ok"] is True
        saved_judgement = json.loads(
            (paths.attempts / str(judge_attempt["attempt_id"]) / "schema_judgement.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved_judgement == {"decision": "pass", "missing_requirements": []}

    structured_input = {
        "schema_outline": schema_outline,
        "sources": [source],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/structured_draft.json",
        "work_dir": "/.knowcoder_workspace/intermediate/sources",
        "workspace_context": {},
    }
    with _active_attempt(layout, paths, "structured_extract", structured_input) as structured_attempt:
        batch_path = paths.attempts / str(structured_attempt["attempt_id"]) / "structured_batches.json"
        batch_path.write_text(
            json.dumps(
                {
                    "batches": [
                        {
                            "entities": [
                                {
                                    "type": "Person",
                                    "id": "person-ada",
                                    "name": "Ada",
                                    "attributes": {},
                                },
                                {
                                    "type": "Company",
                                    "id": "company-analytical-engines",
                                    "name": "Analytical Engines Ltd",
                                    "attributes": {},
                                },
                            ],
                            "relations": [
                                {
                                    "type": "employers",
                                    "head": {"type": "Person", "id": "person-ada"},
                                    "tail": {"type": "Company", "id": "company-analytical-engines"},
                                    "attributes": {},
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        extraction = _decoded(append_instances_batches_from_file.invoke({}))
        assert extraction["ok"] is True
        structured_draft = paths.attempts / structured_attempt["attempt_id"] / "structured_draft.json"

        finalized = finalize_workspace(
            paths=paths,
            schema_source=schema_draft.read_text(encoding="utf-8"),
            draft_paths=[structured_draft],
            sources=[source],
            schema_version=1,
                data_version=1,
                readme=WORKSPACE_README,
            )

    assert finalized["entity_count"] == 2
    assert finalized["relation_count"] == 1
    assert sorted(path.name for path in paths.workspace.iterdir()) == [
        "README.md",
        "data",
        "knowledge",
        "ontology",
        "workspace.yaml",
    ]


def test_evidence_save_binds_runtime_search_sources(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-runtime-evidence-binding", create=True)
    step = "Collect comparable values."
    stage_input = {
        "question": "Compare records.",
        "steps": [step],
        "upload_paths": [],
        "research_dir": "/.knowcoder_workspace/intermediate",
        "workspace_context": {"uncovered_step_indexes": [1]},
    }
    with _active_attempt(layout, paths, "evidence", stage_input) as attempt:
        register_source_record(
            paths.root,
            "web_crawls",
            {"source_id": "source-runtime", "source_kind": "web_crawl", "title": "Runtime source"},
        )
        SearchLedger(paths, attempt["attempt_id"]).append(
            {
                "signature": "runtime-source",
                "step_index": 1,
                "status": "completed",
                "response": {
                    "coverage_binding": {
                        "step_index": 1,
                        "source_ids": ["source-runtime"],
                        "chunk_refs": [
                            {"source_id": "source-runtime", "chunk_id": "source-runtime#chunk_0001"}
                        ],
                    }
                },
            }
        )
        result = _decoded(
            save_evidence_manifest.invoke(
                {
                    "coverage": [{
                        "step_index": 1,
                        "status": "covered",
                        "note": "The runtime should ignore this explanatory field.",
                    }],
                    "unresolved_gaps": ["No remaining gap.", "No remaining gap."],
                }
            )
        )
        manifest = json.loads(
            (paths.attempts / attempt["attempt_id"] / "evidence_manifest.json").read_text(encoding="utf-8")
        )

    assert result["ok"] is True
    assert manifest["coverage"][0]["source_ids"] == ["source-runtime"]
    assert manifest["coverage"][0]["chunk_refs"] == [
        {"source_id": "source-runtime", "chunk_id": "source-runtime#chunk_0001"}
    ]
    assert manifest["unresolved_gaps"] == ["No remaining gap."]
    assert [item["source_id"] for item in manifest["sources"]] == ["source-runtime"]


def test_unstructured_append_allows_empty_records_when_sources_processed(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-empty-records-ok-1234", create=True)
    records = []
    for source_id in ("source-person", "source-company"):
        source_path = paths.sources / f"{source_id}.md"
        source_path.write_text(f"Content from {source_id}.", encoding="utf-8")
        records.append(
            SourceRepository(paths).register(
                "web_search",
                {
                    "source_id": source_id,
                    "source_kind": "web",
                    "file_path": virtual_session_path(source_path.relative_to(paths.root).as_posix()),
                    "file_type": "md",
                    "title": source_id,
                },
            )
        )
    stage_input = {
        "schema_outline": parse_schema(SCHEMA_SOURCE).outline(),
        "sources": records,
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {},
    }
    with _active_attempt(layout, paths, "extract", stage_input):
        result = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [],
                    "relations": [],
                }
            )
        )
    assert result["ok"] is True
    assert result["processed_source_ids"] == ["source-person", "source-company"]
    assert result["entity_count"] == 0
    assert result["relation_count"] == 0
