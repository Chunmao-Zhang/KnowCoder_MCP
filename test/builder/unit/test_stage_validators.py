from __future__ import annotations

from knowcoder_workspace_builder.validation.incremental import MAX_UNIT_ATTEMPTS, validate_incremental_unit
from knowcoder_workspace_builder.validation.validators import get_completion_validator


def test_schema_judge_completion_light_pass() -> None:
    outcome = get_completion_validator("schema_judge").validate(
        {"decision": "pass", "missing_requirements": []}
    )
    assert outcome.ok is True
    assert "repair_prompt" in outcome.to_dict()


def test_schema_judge_completion_revise_requires_missing() -> None:
    outcome = get_completion_validator("schema_judge").validate(
        {"decision": "revise", "missing_requirements": []}
    )
    assert outcome.ok is False
    assert "missing" in outcome.errors[0].casefold()
    assert outcome.repair_prompt


def test_extract_incremental_accepts_schema_independent_instance_type() -> None:
    outline = {
        "entities": [
            {"entity_type": "Team", "attributes": [{"name": "nation"}], "relations": []},
        ]
    }
    outcome = validate_incremental_unit(
        "extract",
        {
            "entities": [
                {
                    "type": "Club",
                    "id": "c1",
                    "name": "X",
                    "attributes": {},
                    "source_refs": ["s1"],
                }
            ],
            "relations": [],
            "processed_source_ids": ["s1"],
        },
        context={"schema_outline": outline, "expected_source_ids": ["s1"], "unit_id": "s1"},
    )
    assert outcome.ok is True
    assert MAX_UNIT_ATTEMPTS == 3


def test_unstructured_completion_allows_processed_sources_without_records() -> None:
    outcome = get_completion_validator("extract").validate(
        {"processed_source_ids": ["s1"], "entity_count": 0, "relation_count": 0},
        context={"sources": [{"source_id": "s1"}]},
    )
    assert outcome.ok is True


def test_structured_completion_allows_no_matching_rows() -> None:
    outcome = get_completion_validator("structured_extract").validate(
        {"processed_source_ids": ["s1"], "entity_count": 0, "relation_count": 0},
        context={"sources": [{"source_id": "s1"}]},
    )
    assert outcome.ok is True
