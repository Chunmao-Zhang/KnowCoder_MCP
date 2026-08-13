"""Mechanical schema repairs and missing-artifact validation hints."""

from __future__ import annotations

from pathlib import Path

from knowcoder_workspace_builder.validation.artifact_validators import SchemaBuildArtifactValidator
from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt


def test_missing_schema_artifact_points_to_save_schema(tmp_path: Path) -> None:
    missing = tmp_path / "schema_draft.py"
    result = SchemaBuildArtifactValidator().validate_path(missing, stage_input={})
    assert result.ok is False
    assert "save_schema" in result.errors[0]
    assert result.outcome.context.get("required_tool") == "save_schema"


def test_missing_artifact_repair_prompt_mentions_persistence_tool() -> None:
    prompt = resolve_repair_prompt(
        "schema_judge",
        mode="completion",
        errors=["The required schema_judge artifact does not exist at schema_judgement.json."],
        context={},
    )
    assert "save_schema_judgement" in prompt
