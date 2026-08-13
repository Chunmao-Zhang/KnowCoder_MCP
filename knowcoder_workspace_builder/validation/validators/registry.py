"""Validator registry for completion and incremental modes."""

from __future__ import annotations

from .base import BaseValidator
from .completion_validators import (
    EvidenceCompletionValidator,
    ExtractCompletionValidator,
    ProblemCompletionValidator,
    SchemaBuildCompletionValidator,
    SchemaJudgeCompletionValidator,
    StructuredExtractCompletionValidator,
    WorkspaceDocumentCompletionValidator,
)
from .incremental_validators import (
    EvidenceIncrementalValidator,
    ExtractIncrementalValidator,
    SchemaBuildIncrementalValidator,
    StructuredExtractIncrementalValidator,
)


_COMPLETION: dict[str, type[BaseValidator]] = {
    "problem": ProblemCompletionValidator,
    "evidence": EvidenceCompletionValidator,
    "schema_build": SchemaBuildCompletionValidator,
    "schema_judge": SchemaJudgeCompletionValidator,
    "extract": ExtractCompletionValidator,
    "structured_extract": StructuredExtractCompletionValidator,
    "document": WorkspaceDocumentCompletionValidator,
}

_INCREMENTAL: dict[str, type[BaseValidator]] = {
    "evidence": EvidenceIncrementalValidator,
    "schema_build": SchemaBuildIncrementalValidator,
    "extract": ExtractIncrementalValidator,
    "structured_extract": StructuredExtractIncrementalValidator,
}


def get_completion_validator(stage: str) -> BaseValidator:
    cls = _COMPLETION.get(stage)
    if cls is None:
        raise KeyError(f"No completion validator for stage: {stage}")
    return cls()


def get_incremental_validator(stage: str) -> BaseValidator:
    cls = _INCREMENTAL.get(stage)
    if cls is None:
        raise KeyError(f"No incremental validator for stage: {stage}")
    return cls()


def get_artifact_validator(stage: str) -> BaseValidator:
    """Return the file-backed final validator for one Builder stage."""
    from knowcoder_workspace_builder.validation.artifact_validators import get_artifact_validator as resolve

    return resolve(stage)
