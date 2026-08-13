"""Offline tool test: serial step append merges then completes."""

from __future__ import annotations

from pathlib import Path

from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.sources import SourceRepository
from knowcoder_workspace_builder.storage.transaction import read_json
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_session_path
from knowcoder_workspace_builder.tools.stage_artifacts import append_instances_batch
from test.builder.unit.test_builder_tools import SCHEMA_SOURCE, _active_attempt, _decoded


def test_incremental_semantic_batches_merge_and_runtime_completes(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-serial-steps-1234", create=True)
    records = []
    for source_id, text in (
        ("source-person", "Ada Lovelace works as an engineer."),
        ("source-company", "Analytical Engines Ltd employs Ada Lovelace."),
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
    units = [
        {"step_index": 1, "step": "Collect person", "source_ids": ["source-person"], "requirements": ["person"]},
        {"step_index": 2, "step": "Collect company", "source_ids": ["source-company"], "requirements": ["company"]},
    ]
    stage_input = {
        "schema_outline": parse_schema(SCHEMA_SOURCE).outline(),
        "sources": records,
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/unstructured_draft.json",
        "workspace_context": {"extraction_units": units, "extraction_mode": "serial_steps"},
    }

    with _active_attempt(layout, paths, "extract", stage_input) as attempt:
        first = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [
                        {
                            "type": "Person",
                            "id": "person-ada",
                            "name": "Ada Lovelace",
                            "attributes": {},
                        }
                    ],
                    "relations": [],
                }
            )
        )
        assert first["ok"] is True
        assert first["extraction_complete"] is True
        assert first["processed_source_ids"] == ["source-person", "source-company"]

        second = _decoded(
            append_instances_batch.invoke(
                {
                    "entities": [
                        {
                            "type": "Person",
                            "id": "person-ada-alias",
                            "name": "Ada Lovelace",
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
                            "head": {"type": "Person", "id": "person-ada-alias"},
                            "tail": {"type": "Company", "id": "company-analytical-engines"},
                            "attributes": {},
                        }
                    ],
                }
            )
        )
        assert second["ok"] is True
        assert second["extraction_complete"] is True
        assert set(second["processed_source_ids"]) == {"source-person", "source-company"}
        assert second["entity_count"] == 2
        assert second["relation_count"] == 1

        draft = read_json(paths.attempts / str(attempt["attempt_id"]) / "unstructured_draft.json")
        assert len(draft["entities"]) == 2
        person = next(item for item in draft["entities"] if item["type"] == "Person")
        assert person["id"] == "person-ada"
        assert set(person["source_refs"]) == {"source-person", "source-company"}
        assert draft["relations"][0]["head"]["id"] == "person-ada"
