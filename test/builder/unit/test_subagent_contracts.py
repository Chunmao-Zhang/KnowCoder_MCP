"""Stage handoff fixtures and validator acceptance for every Subagent contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.service.coordinator import (
    _already_processed_source_ids,
    _sources_excluding,
    schema_data_manifest,
)
from knowcoder_workspace_builder.service.follow_up import FollowUpPlan
from knowcoder_workspace_builder.service.stage_runner import StageRunner
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.validation.prompt_contracts import check_prompt_contracts
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS, validate_stage_result
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.stages import Stage


BUILDER_ROOT = Path(__file__).resolve().parents[3] / "knowcoder_workspace_builder"


MINIMAL_SCHEMA = """
from typing import List, Optional


class Entity:
    _id: str
    name: str


class Company(Entity):
    \"\"\"A company represented by sourced measurements.\"\"\"
    _id: str
    name: str
    annual_revenue: float
    revenue_unit: str


class Measurement(Entity):
    \"\"\"A sourced measurement for one company.\"\"\"
    _id: str
    name: str
    measurement_subject: Company
    \"\"\"Links the measurement to its measured company.\"\"\"
    period: str
    metric_name: str
    value: float
    unit: str
"""


def test_all_subagent_prompts_pass_publication_style_contracts() -> None:
    result = check_prompt_contracts(BUILDER_ROOT)
    assert result["ok"] is True, result["errors"]
    assert set(result["stages"]) == set(STAGE_PROTOCOLS)


def test_problem_handoff_passes_validator() -> None:
    handoff = {
        "workspace_action": "new",
        "base_workspace_id": "",
        "question": "Compare company A and company B on 2024 revenue growth.",
        "scope": {
            "objects": ["company A", "company B"],
            "period": "2024",
            "metrics": ["revenue growth"],
        },
        "steps": [
            "Collect 2024 revenue for company A",
            "Collect 2024 revenue for company B",
            "Collect year-over-year growth for both companies",
        ],
        "missing_information": [],
    }
    result = StageResult(
        ok=True,
        stage="problem",
        status="completed",
        report="Problem decomposition ready.",
        handoff=handoff,
        artifacts={"problem_review": "/tmp/problem_review.json"},
        errors=(),
    )
    validated = validate_stage_result(result)
    assert validated.handoff["steps"]
    assert validated.handoff["missing_information"] == []


def test_evidence_handoff_passes_validator_with_runtime_source_binding() -> None:
    steps = ["Collect 2024 revenue for company A", "Collect 2024 revenue for company B"]
    handoff = {
        "coverage": [
            {
                "step": steps[0],
                "requirements": ["2024 revenue", "company A"],
                "status": "covered",
                "source_ids": ["src-a"],
            },
            {
                "step": steps[1],
                "requirements": ["2024 revenue", "company B"],
                "status": "covered",
                "source_ids": ["src-b"],
            },
        ],
        "sources": [
            {
                "source_id": "src-a",
                "source_kind": "web",
                "file_path": "/.knowcoder_workspace/intermediate/sources/a.json",
            },
            {
                "source_id": "src-b",
                "source_kind": "web",
                "file_path": "/.knowcoder_workspace/intermediate/sources/b.json",
            },
        ],
        "unresolved_gaps": [],
        "blocking_gaps": [],
    }
    result = StageResult(
        ok=True,
        stage="evidence",
        status="completed",
        report="Evidence coverage ready.",
        handoff=handoff,
        artifacts={"evidence_manifest": "/tmp/evidence.json"},
        errors=(),
    )
    validated = validate_stage_result(result, stage_input={"steps": steps})
    assert len(validated.handoff["coverage"]) == 2


def test_duplicate_relation_names_return_a_clear_schema_error() -> None:
    source = """
class Entity:
    _id: str
    name: str

class Team(Entity):
    \"\"\"A team represented in the schema.\"\"\"
    _id: str
    name: str

class Match(Entity):
    \"\"\"A match involving a team.\"\"\"
    _id: str
    name: str
    team: Team
    \"\"\"Links the match to a participating team.\"\"\"

class Player(Entity):
    \"\"\"A player assigned to a team.\"\"\"
    _id: str
    name: str
    team: Team
    \"\"\"Links the player to their team.\"\"\"
"""
    with pytest.raises(ContractError, match="Relation field names must be unique") as error:
        parse_schema(source)
    assert error.value.detail.context == {
        "relation": "team",
        "first_owner": "Match",
        "second_owner": "Player",
    }


def test_schema_judge_handoff_passes_validator() -> None:
    handoff = {
        "decision": "pass",
        "missing_requirements": [],
    }
    result = StageResult(
        ok=True,
        stage="schema_judge",
        status="completed",
        report="Schema judgement ready.",
        handoff=handoff,
        artifacts={"schema_judgement": "/tmp/judge.json"},
        errors=(),
    )
    validated = validate_stage_result(result, stage_input={"steps": ["Collect revenue", "Collect growth"]})
    assert validated.handoff["decision"] == "pass"



@pytest.mark.parametrize("stage", ["extract", "structured_extract"])
def test_extract_handoff_passes_validator(stage: str) -> None:
    handoff = {
        "processed_source_ids": ["source-1"],
        "entity_count": 2,
        "relation_count": 1,
    }
    artifact = "unstructured_draft" if stage == "extract" else "structured_draft"
    result = StageResult(
        ok=True,
        stage=stage,
        status="completed",
        report="Extraction draft ready.",
        handoff=handoff,
        artifacts={artifact: f"/tmp/{artifact}.json"},
        errors=(),
    )
    validated = validate_stage_result(result)
    assert validated.handoff["entity_count"] == 2


def test_schema_data_manifest_is_compact() -> None:
    manifest = schema_data_manifest(
        {
            "coverage": [
                {
                    "requirements": ["x" * 400, "keep", ""],
                    "status": "covered",
                }
            ],
            "unresolved_gaps": ["y" * 400],
        }
    )
    assert manifest["coverage"][0]["step_index"] == 1
    assert all(len(item) <= 120 for item in manifest["coverage"][0]["requirements"])
    assert all(len(item) <= 120 for item in manifest["unresolved_gaps"])


def test_extract_sources_exclude_already_processed_ids() -> None:
    assigned = [
        {"source_id": "src-a", "file_path": "/a"},
        {"source_id": "src-b", "file_path": "/b"},
        {"source_id": "src-c", "file_path": "/c"},
    ]
    remaining = _sources_excluding(assigned, ["src-a", "src-c"])
    assert [item["source_id"] for item in remaining] == ["src-b"]


def test_already_processed_source_ids_reads_extraction_state() -> None:
    state = BuildState(
        session_id="session-extract-1234",
        question="Compare companies.",
        upload_paths=[],
        stage=str(Stage.EXTRACT),
        status="running",
        extraction={
            "unstructured": {
                "status": "completed",
                "processed_source_ids": ["src-a", "src-a", "src-b"],
                "entity_count": 4,
                "relation_count": 2,
            }
        },
    )
    assert _already_processed_source_ids(state, "unstructured") == ["src-a", "src-b"]
    assert _already_processed_source_ids(state, "structured") == []


def test_instances_follow_up_rebuilds_the_current_instance_layer() -> None:
    state = BuildState(
        session_id="session-follow-up-1234",
        question="Compare companies.",
        upload_paths=[],
        stage=str(Stage.READY),
        status="workspace_ready",
        problem_confirmed=True,
        schema_confirmed=True,
        accepted_attempts={
            "problem": "a1",
            "evidence": "a2",
            "schema_build": "a3",
            "schema_judge": "a4",
            "extract": "a5",
            "structured_extract": "a6",
        },
        extraction={
            "unstructured": {
                "status": "completed",
                "processed_source_ids": ["src-a"],
                "entity_count": 2,
                "relation_count": 1,
            }
        },
    )
    updated = FollowUpPlan.create(
        "Re-extract the newly added source.",
        impacts=["instances"],
    ).apply(state)
    assert updated.stage == Stage.EXTRACT
    assert updated.extraction == {}
    assert updated.replace_instances is True
    assert "extract" not in updated.accepted_attempts


def test_schema_follow_up_clears_extraction() -> None:
    state = BuildState(
        session_id="session-schema-follow-up-1234",
        question="Compare companies.",
        upload_paths=[],
        stage=str(Stage.READY),
        status="workspace_ready",
        problem_confirmed=True,
        schema_confirmed=True,
        extraction={
            "unstructured": {
                "status": "completed",
                "processed_source_ids": ["src-a"],
            }
        },
    )
    updated = FollowUpPlan.create(
        "Add a new schema field for margin.",
        impacts=["schema"],
    ).apply(state)
    assert updated.stage == Stage.SCHEMA_BUILD
    assert updated.extraction == {}
    assert updated.schema_confirmed is False


def test_stage_runner_merges_incremental_extraction_counts() -> None:
    previous = {
        "status": "completed",
        "processed_source_ids": ["src-a"],
        "entity_count": 3,
        "relation_count": 1,
        "artifacts": {"unstructured_draft": "/old.json"},
    }
    merged = StageRunner._merge_extraction_bucket(
        previous,
        handoff={
            "processed_source_ids": ["src-b"],
            "entity_count": 2,
            "relation_count": 2,
        },
        artifacts={"unstructured_draft": "/new.json"},
        status="completed",
    )
    assert merged["processed_source_ids"] == ["src-a", "src-b"]
    assert merged["entity_count"] == 3
    assert merged["relation_count"] == 2
    assert merged["artifacts"]["unstructured_draft"] == "/new.json"


def test_stage_runner_keeps_prior_extraction_when_new_pass_skips() -> None:
    previous = {
        "status": "completed",
        "processed_source_ids": ["src-a"],
        "entity_count": 3,
        "relation_count": 1,
        "artifacts": {"unstructured_draft": "/old.json"},
    }
    merged = StageRunner._merge_extraction_bucket(
        previous,
        handoff={
            "processed_source_ids": [],
            "entity_count": 0,
            "relation_count": 0,
            "skip_reason": "No sources provided: the sources list is empty.",
        },
        artifacts={},
        status="skipped",
    )
    assert merged["status"] == "completed"
    assert merged["processed_source_ids"] == ["src-a"]
    assert merged["entity_count"] == 3


def test_minimal_schema_fixture_is_valid_for_extractor_accuracy_checks() -> None:
    parsed = parse_schema(MINIMAL_SCHEMA)
    assert "Company" in parsed.entity_names
    assert "Measurement" in parsed.entity_names
    assert "measurement_subject" in parsed.relation_names
