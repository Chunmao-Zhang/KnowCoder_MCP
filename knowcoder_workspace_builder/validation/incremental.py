"""Incremental unit validation and retry policy."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.validation.validators import ValidationOutcome, get_incremental_validator


MAX_UNIT_ATTEMPTS = 3


def validate_incremental_unit(stage: str, payload: dict[str, Any], *, context: dict[str, Any] | None = None) -> ValidationOutcome:
    validator = get_incremental_validator(stage)
    return validator.validate(payload, context=context)
