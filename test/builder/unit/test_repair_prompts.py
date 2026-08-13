from __future__ import annotations

from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt


def test_completion_schema_unsupported_field_type_case() -> None:
    text = resolve_repair_prompt(
        "schema_build",
        mode="completion",
        errors=['Schema field has an unsupported type; context={"annotation": "Entity", "field": "measurement_subject"}'],
    )
    assert "unsupported_field_types" in text
    assert "semantic relation endpoint" in text


def test_incremental_extract_record_shape_case() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="incremental",
        errors=["Entity attributes must be an object"],
    )
    assert "record_shape" in text
    assert "canonical Instance field" in text
    assert "incremental" in text


def test_incremental_extract_relation_endpoint_case() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="incremental",
        errors=["Relation endpoints must be objects"],
    )
    assert "record_shape" in text
    assert "canonical Instance field" in text


def test_completion_extract_json_attributes_case() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="completion",
        errors=["Instance attributes must contain JSON-compatible values"],
    )
    assert "attributes" in text
    assert "JSON-compatible object" in text


def test_incremental_extract_empty_unit_rebuilds_consolidated_batch() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="incremental",
        errors=["Extract unit is empty"],
    )
    assert "empty_unit" in text
    assert "Runtime marks the assigned sources processed" in text
    assert "every extracted entity and relation" in text


def test_completion_evidence_no_source_case() -> None:
    text = resolve_repair_prompt(
        "evidence",
        mode="completion",
        errors=["Successful evidence coverage requires at least one source"],
    )
    assert "no_sources" in text


def test_completion_extract_missing_sources_case() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="completion",
        errors=["Extraction draft must cover every assigned source"],
        context={"missing": ["source-2"]},
    )
    assert "missing_sources" in text
    assert "empty record lists" in text
    assert "source-2" in text


def test_completion_extract_source_refs_case() -> None:
    text = resolve_repair_prompt(
        "extract",
        mode="completion",
        errors=["Instance record references an unassigned source"],
    )
    assert "source_refs" in text
    assert "Runtime injects source_refs" in text


def test_completion_structured_extract_missing_artifact_case() -> None:
    text = resolve_repair_prompt(
        "structured_extract",
        mode="completion",
        errors=["Subagent completion is missing its stage artifact: structured_draft"],
    )
    assert "missing_artifact" in text
    assert "append_instances_batches_from_file" in text
