"""File-backed validation for Builder stage artifacts.

The model-owned artifact is the only stage input considered authoritative.  The
chat completion is deliberately outside this module: callers provide the
candidate path and receive a normalized validation result that can be used to
continue or finish the active attempt.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import BuilderError, ContractError
from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.stage_artifacts import artifact_path
from knowcoder_workspace_builder.storage.transaction import read_json
from knowcoder_workspace_builder.storage.readme import validate_workspace_readme
from knowcoder_workspace_builder.validation.file_validation import (
    MAX_VALIDATION_ROUNDS,
    STAGE_CANDIDATE_FILES,
    STAGE_PERSISTENCE_TOOLS,
    record_validation_round,
)
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS
from knowcoder_workspace_builder.validation.validators.base import BaseValidator, ValidationMode, ValidationOutcome
from knowcoder_workspace_builder.validation.validators.registry import get_completion_validator


@dataclass(frozen=True)
class ArtifactValidation:
    """One deterministic validation pass over one candidate artifact."""

    stage: str
    path: Path | None
    outcome: ValidationOutcome
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome.ok

    @property
    def errors(self) -> list[str]:
        return list(self.outcome.errors)

    @property
    def repair_prompt(self) -> str:
        return self.outcome.repair_prompt

    @property
    def retryable(self) -> bool:
        return str(self.outcome.context.get("reason") or "") in {
            "missing_artifact",
            "empty_artifact",
            "invalid_json",
            "model_output_invalid",
        }

    def feedback(self) -> dict[str, Any]:
        path = ""
        if self.path is not None:
            try:
                path = virtual_path_for(active_session_paths().root, self.path)
            except Exception:
                path = str(self.path)
        return {
            "stage": self.stage,
            "candidate_path": path,
            "errors": self.errors,
            "context": dict(self.outcome.context),
            "repair_prompt": self.repair_prompt,
        }

    def to_stage_result(self, *, stage_input: dict[str, Any]) -> StageResult:
        protocol = STAGE_PROTOCOLS[self.stage]
        if not self.ok:
            return StageResult(
                ok=False,
                stage=self.stage,
                status="failed",
                report=self.errors[0] if self.errors else f"{self.stage} artifact validation failed.",
                handoff={"validation_feedback": self.feedback()},
                errors=tuple(self.errors or [f"{self.stage} artifact validation failed."]),
            )

        skipped = self.stage in {"extract", "structured_extract"} and not stage_input.get("sources")
        status = "skipped" if skipped else "completed"
        artifacts: dict[str, str] = {}
        if not skipped and self.path is not None:
            artifact_name = protocol.artifact_fields[0]
            artifacts[artifact_name] = virtual_path_for(active_session_paths().root, self.path)
        handoff = dict(self.payload)
        if skipped:
            handoff = {
                "processed_source_ids": [],
                "entity_count": 0,
                "relation_count": 0,
                "skip_reason": "No sources were assigned.",
            }
        report = {
            "problem": "Problem decomposition completed.",
            "evidence": "Data collection completed.",
            "schema_build": "Schema construction completed.",
            "schema_judge": (
                "Schema review passed."
                if handoff.get("decision") == "pass"
                else "Schema revision requested."
            ),
            "extract": "Unstructured data extraction completed.",
            "structured_extract": "Structured data extraction completed.",
            "document": "Workspace documentation completed.",
        }[self.stage]
        return StageResult(
            ok=True,
            stage=self.stage,
            status=status,
            report=report,
            handoff=handoff,
            artifacts=artifacts,
            errors=(),
            limitations=tuple(self.outcome.context.get("limitations") or []),
            metrics=dict(self.outcome.context.get("metrics") or {}),
        )


class ArtifactValidator(BaseValidator, ABC):
    """Common file validator contract used by every Builder stage."""

    mode = ValidationMode.ARTIFACT
    max_attempts = MAX_VALIDATION_ROUNDS

    def validate_path(
        self,
        path: Path,
        *,
        stage_input: dict[str, Any],
    ) -> ArtifactValidation:
        if not path.is_file():
            save_tool = STAGE_PERSISTENCE_TOOLS.get(self.stage, "the stage persistence tool")
            outcome = self.failure(
                [
                    (
                        f"The required {self.stage} artifact does not exist at {path.name}. "
                        f"Call {save_tool} successfully so the candidate is written to disk. "
                        "Validation only inspects the saved file."
                    )
                ],
                context={
                    "candidate_path": str(path),
                    "reason": "missing_artifact",
                    "required_tool": save_tool,
                    "repair_hint": (
                        f"Persist the complete {self.stage} candidate with {save_tool} before finishing. "
                        "Do not rely on chat text. The runtime validates only the saved file."
                    ),
                },
            )
            return ArtifactValidation(self.stage, path, outcome)
        payload: Any = {}
        try:
            payload = self._read(path)
            outcome = self.validate_file(path, payload, stage_input=stage_input)
            if not outcome.ok:
                outcome.context.setdefault("reason", "model_output_invalid")
        except (BuilderError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            message = exc.detail.message if isinstance(exc, BuilderError) else str(exc)
            context = dict(exc.detail.context) if isinstance(exc, BuilderError) else {}
            if isinstance(exc, json.JSONDecodeError) or isinstance(exc.__cause__, json.JSONDecodeError):
                context["reason"] = "invalid_json"
            elif "empty" in message.casefold():
                context["reason"] = "empty_artifact"
            elif isinstance(exc, (ContractError, TypeError, ValueError)):
                context.setdefault("reason", "model_output_invalid")
            else:
                context.setdefault("reason", "system_error")
            outcome = self.failure([message], context=context)
            payload = {}
        normalized = self.result_payload(payload) if isinstance(payload, dict) else {}
        return ArtifactValidation(self.stage, path, outcome, normalized)

    def result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    @abstractmethod
    def _read(self, path: Path) -> Any:
        """Read the stage-specific candidate file."""

    @abstractmethod
    def validate_file(
        self,
        path: Path,
        payload: Any,
        *,
        stage_input: dict[str, Any],
    ) -> ValidationOutcome:
        """Validate a parsed candidate and return the shared outcome type."""


def _completion(stage: str, payload: dict[str, Any], stage_input: dict[str, Any]) -> ValidationOutcome:
    context: dict[str, Any] = {
        "steps": list(stage_input.get("steps") or []),
        "sources": list(stage_input.get("sources") or []),
        "upload_paths": list(stage_input.get("upload_paths") or []),
        "schema_outline": stage_input.get("schema_outline"),
        "workspace_context": stage_input.get("workspace_context"),
        "status": "completed",
    }
    if stage in {"extract", "structured_extract"}:
        context["draft"] = payload.get("_draft")
        payload = {key: value for key, value in payload.items() if key != "_draft"}
    return get_completion_validator(stage).validate(payload, context=context)


class ProblemArtifactValidator(ArtifactValidator):
    stage = "problem"

    def _read(self, path: Path) -> Any:
        return read_json(path)

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Problem artifact must be a JSON object."])
        return _completion(self.stage, payload, stage_input)


class EvidenceArtifactValidator(ArtifactValidator):
    stage = "evidence"

    def _read(self, path: Path) -> Any:
        return read_json(path)

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Evidence artifact must be a JSON object."])
        return _completion(self.stage, payload, stage_input)


class SchemaBuildArtifactValidator(ArtifactValidator):
    stage = "schema_build"

    def _read(self, path: Path) -> Any:
        source = path.read_text(encoding="utf-8").strip()
        if not source:
            raise ContractError("Schema artifact is empty")
        parsed = parse_schema(source, require_relations=False)
        return {"schema_source": source, "schema_outline": parsed.outline()}

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Schema artifact is invalid."])
        return _completion(self.stage, payload, stage_input)


class SchemaJudgeArtifactValidator(ArtifactValidator):
    stage = "schema_judge"

    def _read(self, path: Path) -> Any:
        return read_json(path)

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Schema judgement artifact must be a JSON object."])
        return _completion(self.stage, payload, stage_input)


class _ExtractArtifactValidator(ArtifactValidator):
    def _read(self, path: Path) -> Any:
        return read_json(path)

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Extraction artifact must be a JSON object."])
        draft = dict(payload)
        handoff = {
            "processed_source_ids": list(draft.get("processed_source_ids") or []),
            "entity_count": len(draft.get("entities") or []),
            "relation_count": len(draft.get("relations") or []),
            "_draft": draft,
        }
        return _completion(self.stage, handoff, stage_input)

    def result_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "processed_source_ids": list(payload.get("processed_source_ids") or []),
            "entity_count": len(payload.get("entities") or []),
            "relation_count": len(payload.get("relations") or []),
        }


class ExtractArtifactValidator(_ExtractArtifactValidator):
    stage = "extract"


class StructuredExtractArtifactValidator(_ExtractArtifactValidator):
    stage = "structured_extract"


def _readme_payload(text: str) -> dict[str, Any]:
    parsed = validate_workspace_readme(text)
    body = text.split("---", 2)[-1]
    overview = body.split("# Workspace Overview", 1)[-1].split("- Mode:", 1)[0].strip()
    guidance = body.split("## Incremental Extension", 1)[-1].strip()
    return {
        "name": parsed["name"],
        "description": parsed["description"],
        "summary": overview,
        "incremental_guidance": guidance,
    }


class DocumentArtifactValidator(ArtifactValidator):
    stage = "document"

    def _read(self, path: Path) -> Any:
        return _readme_payload(path.read_text(encoding="utf-8"))

    def validate_file(self, path: Path, payload: Any, *, stage_input: dict[str, Any]) -> ValidationOutcome:
        if not isinstance(payload, dict):
            return self.failure(["Workspace README artifact is invalid."])
        return _completion(self.stage, payload, stage_input)


_VALIDATORS: dict[str, type[ArtifactValidator]] = {
    "problem": ProblemArtifactValidator,
    "evidence": EvidenceArtifactValidator,
    "schema_build": SchemaBuildArtifactValidator,
    "schema_judge": SchemaJudgeArtifactValidator,
    "extract": ExtractArtifactValidator,
    "structured_extract": StructuredExtractArtifactValidator,
    "document": DocumentArtifactValidator,
}


def get_artifact_validator(stage: str) -> ArtifactValidator:
    validator = _VALIDATORS.get(stage)
    if validator is None:
        raise KeyError(f"No artifact validator for stage: {stage}")
    return validator()


def validate_current_artifact(
    stage: str,
    *,
    stage_input: dict[str, Any],
    validation_round: int,
) -> ArtifactValidation:
    """Validate the active attempt's fixed candidate and record the result."""
    context = active_invocation_context()
    paths = active_session_paths()
    name, suffix = STAGE_CANDIDATE_FILES[stage]
    candidate = artifact_path(paths, context.attempt_id, name, suffix)
    result = get_artifact_validator(stage).validate_path(candidate, stage_input=stage_input)
    feedback = result.feedback()
    record_validation_round(
        stage,
        round_index=validation_round,
        ok=result.ok,
        errors=result.errors,
        candidate_path=feedback.get("candidate_path", ""),
        repair_prompt=result.repair_prompt,
        feedback=feedback,
    )
    return result
