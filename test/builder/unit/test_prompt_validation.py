from __future__ import annotations

from pathlib import Path

import pytest

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.validation.inputs import validate_stage_input
from knowcoder_workspace_builder.validation.prompt_contracts import check_prompt_contracts
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS, validate_stage_result
from knowcoder_workspace_builder.validation.validators import get_completion_validator


BUILDER_ROOT = Path(__file__).resolve().parents[3] / "knowcoder_workspace_builder"


def _input_for(stage: str) -> dict[str, object]:
    values: dict[str, object] = {
        "question": "Compare the supplied records.",
        "upload_paths": [],
        "current_date": "2026-07-16",
        "workspace_context": {},
        "problem": {"steps": ["Compare the requested records."]},
        "steps": ["Compare the requested records."],
        "research_dir": ".knowcoder_workspace/sessions/session-1234/intermediate",
        "data_manifest": {"sources": []},
        "schema_source": "class Entity: ...",
        "schema_outline": {"entities": []},
        "sources": [],
        "draft_path": ".knowcoder_workspace/sessions/session-1234/intermediate/attempts/draft.json",
        "work_dir": ".knowcoder_workspace/sessions/session-1234/intermediate/sources",
        "batch_path": ".knowcoder_workspace/sessions/session-1234/intermediate/attempts/structured_batches.json",
        "instance_summary": {"entity_count": 1, "relation_count": 0},
        "artifact_index": {},
    }
    return {field: values[field] for field in STAGE_PROTOCOLS[stage].input_fields}


def _successful_result_for(stage: str) -> dict[str, object]:
    protocol = STAGE_PROTOCOLS[stage]
    handoff: dict[str, object] = {field: [] for field in protocol.handoff_fields}
    if stage == "problem":
        handoff.update(
            workspace_action="new",
            base_workspace_id="",
            question="Compare records.",
            scope={},
            steps=["Compare records."],
            missing_information=[],
        )
    elif stage == "evidence":
        handoff.update(
            coverage=[
                {
                    "step": "Compare the requested records.",
                    "requirements": ["requested facts"],
                    "source_ids": ["source-1"],
                    "status": "covered",
                }
            ],
            sources=[{"source_id": "source-1"}],
            unresolved_gaps=[],
            blocking_gaps=[],
        )
    elif stage == "schema_build":
        handoff.update(schema_source="class Entity: ...", schema_outline={})
    elif stage == "schema_judge":
        handoff.update(
            decision="pass",
            missing_requirements=[],
            coverage=[
                {
                    "step": "Compare the requested records.",
                    "status": "supported",
                    "support": ["The schema contains the required facts."],
                    "missing": [],
                }
            ],
        )
    elif stage == "document":
        handoff.update(
            name="Research Workspace",
            description="A reusable research Workspace.",
            summary="Contains validated research records.",
            incremental_guidance="Extend the existing files for new requirements.",
        )
    else:
        handoff.update(processed_source_ids=[], entity_count=0, relation_count=0)
    return {
        "ok": True,
        "stage": stage,
        "status": "completed",
        "report": f"{stage} completed.",
        "handoff": handoff,
        "artifacts": {field: f".knowcoder_workspace/{field}.json" for field in protocol.artifact_fields},
    }


def test_all_subagent_prompts_match_executable_contracts() -> None:
    result = check_prompt_contracts(BUILDER_ROOT)

    assert result["ok"] is True
    assert result["stages"] == sorted(STAGE_PROTOCOLS)
    schema_prompt = (BUILDER_ROOT / "subagents" / "schema_builder" / "AGENT.md").read_text(encoding="utf-8")
    assert "Keep entity names in PascalCase and relation names unique and owner-prefixed." in schema_prompt
    assert "Give every entity and every relation a short non-empty description." in schema_prompt
    assert "`str`, `int`, `float`, or `bool`" in schema_prompt
    assert "`entities`" in schema_prompt
    assert "`relations`" in schema_prompt
    problem_prompt = (BUILDER_ROOT / "subagents" / "problem_clarifier" / "AGENT.md").read_text(encoding="utf-8")
    assert "Use the fewest steps that completely cover the answerable research scope." in problem_prompt
    assert "Before saving, audit every name, source, count, date, metric, topic, and expected result" in problem_prompt
    assert "Prefer 8 to 15 steps" not in problem_prompt
    assert "Austin" not in problem_prompt


def test_all_subagent_core_prompts_use_short_positive_directives() -> None:
    expected_sections = [
        "## Task Definition",
        "## Context",
        "## Operating Protocol",
        "## File Contract",
        "## Quality Standard",
        "## Tools",
        "## Examples",
    ]
    banned_phrases = ("do not", "don't", "never", "avoid ", "omit ", "leave ", " out of ")
    for path in sorted((BUILDER_ROOT / "subagents").glob("*/AGENT.md")):
        prompt = path.read_text(encoding="utf-8")
        assert len([line for line in prompt.splitlines() if line.startswith("# ")]) == 1
        assert [line for line in prompt.splitlines() if line.startswith("## ")] == expected_sections
        assert "# Workflow" not in prompt
        core = prompt.split("## Examples", 1)[0].casefold()
        assert all(phrase not in core for phrase in banned_phrases), path
        assert all(len(line) <= 120 for line in core.splitlines()), path


def test_structured_extractor_prompt_defines_canonical_relation_endpoints() -> None:
    prompt = (BUILDER_ROOT / "subagents" / "structured_data_extractor" / "AGENT.md").read_text(encoding="utf-8")
    core = prompt

    assert "Write `type` and `id` inside both relation endpoint" in core
    assert "Finish with a short acknowledgement after the persistence tool returns `ok=true`." in core
    assert "`workspace_snapshot` contains the current README and accepted artifacts." in core
    assert "output examples for naming, IDs, attributes, and relation endpoints." in core
    assert "Determine what one row represents and which columns identify that record." in core
    assert "Make the first `write_file` action create the production parser" in core
    assert "Track total rows, converted rows, merged duplicate rows, and skipped rows" in core
    assert "summary accounts for every source row and contains the written `batch_path`" in core
    assert "Apply every execution or validation error together" in core
    assert "read every row from the full assigned files" in core
    assert "Call `append_instances_batches_from_file` as the next tool action" in core
    assert "strong source identifiers" in core
    assert "Keep conflicting identities separate." in core


def test_unstructured_extraction_prompt_lives_in_the_batch_tool() -> None:
    from knowcoder_workspace_builder.tools.unstructured_extractor import EXTRACTION_SYSTEM_PROMPT

    controller = (BUILDER_ROOT / "subagents" / "data_extractor" / "AGENT.md").read_text(encoding="utf-8")

    assert "Call `extract_unstructured_chunks` for the current extraction stage." in controller
    assert "Each entity contains type, id, name, and attributes." in EXTRACTION_SYSTEM_PROMPT
    assert "Each relation contains type, head, tail, and attributes." in EXTRACTION_SYSTEM_PROMPT
    assert "Include every relation endpoint in entities" in EXTRACTION_SYSTEM_PROMPT
    assert "Use the Schema outline to determine the target entity types" in EXTRACTION_SYSTEM_PROMPT
    assert "research step" not in EXTRACTION_SYSTEM_PROMPT.casefold()


def test_schema_prompt_matches_supported_relation_cardinality() -> None:
    prompt = (BUILDER_ROOT / "subagents" / "schema_builder" / "AGENT.md").read_text(encoding="utf-8")

    assert "Give each relation `name`, `head`, `tail`, `description`, and boolean `many` and `optional`." in prompt
    assert "The runtime compiles the Python Schema" in prompt


def test_schema_judger_scopes_review_edits_to_the_accepted_baseline() -> None:
    prompt = (BUILDER_ROOT / "subagents" / "schema_judger" / "AGENT.md").read_text(encoding="utf-8")
    core = prompt.split("## Examples", 1)[0]

    assert "reuse the accepted baseline judgement" in core
    assert "trace the requested edit and every path it touches" in core
    assert "verify each preservation constraint stated in the user instruction" in core


def test_schema_prompts_require_concrete_domain_entities() -> None:
    builder = (BUILDER_ROOT / "subagents" / "schema_builder" / "AGENT.md").read_text(encoding="utf-8")
    judge = (BUILDER_ROOT / "subagents" / "schema_judger" / "AGENT.md").read_text(encoding="utf-8")

    assert "Add an entity when records need independent identity" in builder
    assert "Keep distinct domain concepts in distinct entity types." in builder
    assert "Count only concrete domain entities toward object coverage." in judge
    assert "Treat a generic record container as insufficient coverage" in judge
    assert "Observation" not in builder
    assert "Observation" not in judge


@pytest.mark.parametrize("stage", sorted(STAGE_PROTOCOLS))
def test_each_declared_stage_input_is_accepted(stage: str) -> None:
    value = _input_for(stage)

    assert validate_stage_input(stage, value) == value


@pytest.mark.parametrize("stage", sorted(STAGE_PROTOCOLS))
def test_each_stage_rejects_a_missing_declared_input(stage: str) -> None:
    value = _input_for(stage)
    value.pop(STAGE_PROTOCOLS[stage].input_fields[0])

    with pytest.raises(ContractError, match="missing required fields"):
        validate_stage_input(stage, value)


@pytest.mark.parametrize("stage", sorted(STAGE_PROTOCOLS))
def test_each_complete_stage_result_is_accepted(stage: str) -> None:
    result = validate_stage_result(
        _successful_result_for(stage),
        expected_stage=stage,
        stage_input=_input_for(stage),
    )

    assert result.ok is True
    assert result.status == "completed"


@pytest.mark.parametrize("stage", sorted(STAGE_PROTOCOLS))
def test_each_successful_stage_rejects_a_missing_handoff_field(stage: str) -> None:
    value = _successful_result_for(stage)
    handoff = dict(value["handoff"])
    handoff.pop(STAGE_PROTOCOLS[stage].handoff_fields[0])
    value["handoff"] = handoff

    with pytest.raises(ContractError, match="Successful stage result is incomplete"):
        validate_stage_result(value)


def test_failed_stage_requires_an_explicit_error() -> None:
    value = _successful_result_for("evidence")
    value.update(ok=False, status="failed", errors=[])

    with pytest.raises(ContractError, match="requires at least one error"):
        validate_stage_result(value)


@pytest.mark.parametrize("stage", ["extract", "structured_extract"])
def test_empty_extractor_can_skip_with_an_explicit_reason(stage: str) -> None:
    result = validate_stage_result(
        {
            "ok": True,
            "stage": stage,
            "status": "skipped",
            "report": "No matching sources were supplied.",
            "handoff": {"skip_reason": "The source list for this extractor is empty."},
        }
    )

    assert result.status == "skipped"


def test_schema_judgement_rejects_an_undeclared_decision() -> None:
    value = _successful_result_for("schema_judge")
    value["handoff"] = {**dict(value["handoff"]), "decision": "accept"}

    outcome = get_completion_validator("schema_judge").validate(value["handoff"])

    assert outcome.ok is False
    assert "must be pass or revise" in outcome.errors[0]


def test_schema_judgement_light_contract_requires_missing_on_revise() -> None:
    outcome = get_completion_validator("schema_judge").validate({"decision": "revise", "missing_requirements": []})

    assert outcome.ok is False
    assert "at least one missing requirement" in outcome.errors[0]


def test_extraction_completion_requires_every_assigned_source() -> None:
    stage_input = _input_for("extract")
    stage_input["sources"] = [{"source_id": "source-1"}]
    value = _successful_result_for("extract")

    outcome = get_completion_validator("extract").validate(value["handoff"], context=stage_input)

    assert outcome.ok is False
    assert "cover every assigned source" in outcome.errors[0]


def test_evidence_coverage_cannot_reference_an_unknown_source() -> None:
    stage_input = _input_for("evidence")
    value = _successful_result_for("evidence")
    value["handoff"] = {
        "coverage": [
            {
                "step": stage_input["steps"][0],
                "requirements": ["requested facts"],
                "source_ids": ["missing-source"],
                "status": "covered",
            }
        ],
        "sources": [{"source_id": "source-1"}],
        "unresolved_gaps": [],
        "blocking_gaps": [],
    }

    outcome = get_completion_validator("evidence").validate(value["handoff"], context=stage_input)

    assert outcome.ok is False
    assert "unknown sources" in outcome.errors[0]
