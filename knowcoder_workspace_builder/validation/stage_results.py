"""Structural StageResult envelope validation from declared protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import ContractError


@dataclass(frozen=True)
class StageProtocol:
    agent: str
    input_fields: tuple[str, ...]
    handoff_fields: tuple[str, ...]
    artifact_fields: tuple[str, ...]
    prompt_terms: tuple[str, ...] = ()
    model_fields: tuple[str, ...] = ()


STAGE_PROTOCOLS: dict[str, StageProtocol] = {
    "problem": StageProtocol(
        agent="problem_clarifier",
        input_fields=("question", "upload_paths", "current_date", "workspace_context"),
        handoff_fields=(
            "workspace_action",
            "base_workspace_id",
            "question",
            "scope",
            "steps",
            "missing_information",
        ),
        artifact_fields=("problem_review",),
        model_fields=("workspace_action", "base_workspace_id", "scope", "steps", "missing_information"),
    ),
    "evidence": StageProtocol(
        agent="evidence_collector",
        input_fields=("question", "steps", "upload_paths", "research_dir", "workspace_context"),
        handoff_fields=("coverage", "sources", "unresolved_gaps", "blocking_gaps"),
        artifact_fields=("evidence_manifest",),
        prompt_terms=("step_index", "status"),
        model_fields=("coverage", "unresolved_gaps"),
    ),
    "schema_build": StageProtocol(
        agent="schema_builder",
        input_fields=("question", "steps", "data_manifest", "workspace_context"),
        handoff_fields=("schema_source", "schema_outline"),
        artifact_fields=("schema_draft",),
        prompt_terms=("entities", "relations", "description", "attributes"),
        model_fields=("entities", "relations", "remove_entity_names", "remove_relation_names"),
    ),
    "schema_judge": StageProtocol(
        agent="schema_judger",
        input_fields=("question", "steps", "data_manifest", "schema_source", "workspace_context"),
        handoff_fields=("decision", "missing_requirements"),
        artifact_fields=("schema_judgement",),
        prompt_terms=(),
        model_fields=("decision", "missing_requirements"),
    ),
    "extract": StageProtocol(
        agent="data_extractor",
        input_fields=("schema_outline", "sources", "draft_path", "workspace_context"),
        handoff_fields=("processed_source_ids", "entity_count", "relation_count"),
        artifact_fields=("unstructured_draft",),
        model_fields=("entities", "relations"),
    ),
    "structured_extract": StageProtocol(
        agent="structured_data_extractor",
        input_fields=("schema_outline", "sources", "draft_path", "work_dir", "batch_path", "workspace_context"),
        handoff_fields=("processed_source_ids", "entity_count", "relation_count"),
        artifact_fields=("structured_draft",),
        model_fields=("entities", "relations"),
    ),
    "document": StageProtocol(
        agent="workspace_documenter",
        input_fields=(
            "problem",
            "schema_source",
            "instance_summary",
            "sources",
            "artifact_index",
            "workspace_context",
        ),
        handoff_fields=(
            "name",
            "description",
            "summary",
            "incremental_guidance",
        ),
        artifact_fields=("workspace_readme",),
        model_fields=(
            "name",
            "description",
            "summary",
            "incremental_guidance",
        ),
    ),
}


def _text_list(value: Any, *, field: str, stage: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{field} must be a list of non-empty text", stage=stage)
    if not allow_empty and not value:
        raise ContractError(f"{field} cannot be empty", stage=stage)
    return [str(item) for item in value]


def _validate_extraction(result: StageResult, stage_input: dict[str, Any] | None) -> None:
    if result.status == "skipped":
        if stage_input is not None and stage_input.get("sources"):
            raise ContractError("Extraction can be skipped only when its source list is empty", stage=result.stage)
        return
    processed = _text_list(result.handoff.get("processed_source_ids"), field="processed_source_ids", stage=result.stage)
    if len(processed) != len(set(processed)):
        raise ContractError("processed_source_ids must be unique", stage=result.stage)
    for field in ("entity_count", "relation_count"):
        count = result.handoff.get(field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ContractError(f"{field} must be a non-negative integer", stage=result.stage)
    if stage_input is not None:
        expected = {str(item["source_id"]) for item in stage_input.get("sources") or []}
        if set(processed) != expected:
            raise ContractError(
                "Extraction completion must cover every assigned source",
                stage=result.stage,
                missing=sorted(expected - set(processed)),
                unexpected=sorted(set(processed) - expected),
            )


def validate_stage_result(
    value: StageResult | dict[str, Any],
    *,
    expected_stage: str | None = None,
    stage_input: dict[str, Any] | None = None,
) -> StageResult:
    result = value if isinstance(value, StageResult) else StageResult.from_dict(value)
    if expected_stage and result.stage != expected_stage:
        raise ContractError("Stage result belongs to a different stage", expected=expected_stage, actual=result.stage)
    protocol = STAGE_PROTOCOLS.get(result.stage)
    if protocol is None:
        raise ContractError("Unknown stage result contract", stage=result.stage)
    missing_handoff = [field for field in protocol.handoff_fields if field not in result.handoff]
    missing_artifacts = [field for field in protocol.artifact_fields if field not in result.artifacts]
    if result.status == "skipped":
        if not str(result.handoff.get("skip_reason") or "").strip():
            raise ContractError("A skipped stage requires skip_reason", stage=result.stage)
    elif result.ok and (missing_handoff or missing_artifacts):
        raise ContractError(
            "Successful stage result is incomplete",
            stage=result.stage,
            missing_handoff=missing_handoff,
            missing_artifacts=missing_artifacts,
        )
    elif not result.ok and not result.errors:
        raise ContractError("Failed stage result requires at least one error", stage=result.stage)
    if result.ok and result.status == "skipped":
        if result.stage not in {"extract", "structured_extract"}:
            raise ContractError("Only extraction stages may return skipped", stage=result.stage)
        _validate_extraction(result, stage_input)
    return result
