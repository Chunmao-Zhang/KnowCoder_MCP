"""Shared validator base for completion and incremental checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ValidationMode(StrEnum):
    COMPLETION = "completion"
    INCREMENTAL = "incremental"
    ARTIFACT = "artifact"


@dataclass
class ValidationOutcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    repair_prompt: str = ""
    unit_id: str = ""
    skip_unit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "context": dict(self.context),
            "repair_prompt": self.repair_prompt,
            "unit_id": self.unit_id,
            "skip_unit": self.skip_unit,
        }


class BaseValidator:
    """Common helpers for stage completion and incremental unit validators."""

    stage: str = ""
    mode: ValidationMode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        raise NotImplementedError

    def repair_prompt_for(
        self,
        errors: list[str],
        *,
        context: dict[str, Any] | None = None,
    ) -> str:
        from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt

        return resolve_repair_prompt(
            self.stage,
            mode=self.mode.value,
            errors=errors,
            context=context or {},
        )

    @staticmethod
    def require_mapping(value: Any, *, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return value

    @staticmethod
    def text_list(value: Any, *, field: str, allow_empty: bool = True) -> list[str]:
        if value is None:
            value = []
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{field} must be a list of non-empty text")
        items = [item.strip() for item in value]
        if not allow_empty and not items:
            raise ValueError(f"{field} cannot be empty")
        return items

    def failure(
        self,
        errors: list[str],
        *,
        context: dict[str, Any] | None = None,
        unit_id: str = "",
        skip_unit: bool = False,
    ) -> ValidationOutcome:
        ctx = dict(context or {})
        return ValidationOutcome(
            ok=False,
            errors=list(errors),
            context=ctx,
            repair_prompt=self.repair_prompt_for(errors, context=ctx),
            unit_id=unit_id,
            skip_unit=skip_unit,
        )

    def success(self, *, context: dict[str, Any] | None = None, unit_id: str = "") -> ValidationOutcome:
        return ValidationOutcome(ok=True, context=dict(context or {}), unit_id=unit_id)
