"""Incremental unit validators used during multi-step stage execution."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.validation.extraction import validate_extraction_draft
from knowcoder_workspace_builder.validation.validators.base import BaseValidator, ValidationMode, ValidationOutcome


class EvidenceIncrementalValidator(BaseValidator):
    """Validate one evidence unit (one step update) before merge."""

    stage = "evidence"
    mode = ValidationMode.INCREMENTAL

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        ctx = dict(context or {})
        unit_id = str(ctx.get("unit_id") or payload.get("step_index") or "")
        try:
            data = self.require_mapping(payload, label="evidence unit")
            step_index = data.get("step_index")
            if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 1:
                raise ValueError("Evidence unit requires step_index as a positive integer")
            status = data.get("status")
            if status not in {"covered", "limited", "blocked"}:
                raise ValueError("Evidence unit status must be covered, limited, or blocked")
            self.text_list(data.get("requirements"), field="requirements", allow_empty=False)
            # source_ids optional in model unit; runtime binds them. If present and empty while covered, reject.
            if status == "covered" and "source_ids" in data and not data.get("source_ids"):
                raise ValueError("Covered evidence unit requires source evidence from tool calls")
            return self.success(unit_id=str(step_index), context={"step_index": step_index, "status": status})
        except ValueError as exc:
            return self.failure([str(exc)], context=ctx, unit_id=unit_id)


class SchemaBuildIncrementalValidator(BaseValidator):
    """Validate one schema patch unit before merge into draft."""

    stage = "schema_build"
    mode = ValidationMode.INCREMENTAL

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        ctx = dict(context or {})
        unit_id = str(ctx.get("unit_id") or payload.get("unit_id") or "schema-unit")
        try:
            data = self.require_mapping(payload, label="schema unit")
            if data.get("entities") is not None or data.get("relations") is not None:
                entities = data.get("entities") or []
                relations = data.get("relations") or []
                if not isinstance(entities, list) or not isinstance(relations, list):
                    raise ValueError("Schema unit entities/relations must be lists")
                for item in entities:
                    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                        raise ValueError("Each schema unit entity requires a name")
                for item in relations:
                    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                        raise ValueError("Each schema unit relation requires a name")
                return self.success(unit_id=unit_id, context={"entity_count": len(entities), "relation_count": len(relations)})
            raise ValueError("Schema unit requires entities or relations patch fields")
        except (ValueError, ContractError) as exc:
            message = exc.detail.message if isinstance(exc, ContractError) else str(exc)
            err_ctx = dict(ctx)
            if isinstance(exc, ContractError):
                err_ctx.update(exc.detail.context)
            return self.failure([message], context=err_ctx, unit_id=unit_id)


class ExtractIncrementalValidator(BaseValidator):
    """Validate one extraction batch unit before append/merge."""

    stage = "extract"
    mode = ValidationMode.INCREMENTAL

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        return _validate_extract_unit(self, payload, context=context)


class StructuredExtractIncrementalValidator(BaseValidator):
    stage = "structured_extract"
    mode = ValidationMode.INCREMENTAL

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        return _validate_extract_unit(self, payload, context=context)


def _validate_extract_unit(
    validator: BaseValidator,
    payload: Any,
    *,
    context: dict[str, Any] | None,
) -> ValidationOutcome:
    ctx = dict(context or {})
    unit_id = str(ctx.get("unit_id") or "extract-unit")
    try:
        data = validator.require_mapping(payload, label="extract unit")
        entities = data.get("entities") or []
        relations = data.get("relations") or []
        processed = data.get("processed_source_ids") or []
        if not isinstance(entities, list) or not isinstance(relations, list) or not isinstance(processed, list):
            raise ValueError("Extract unit requires entities, relations, and processed_source_ids lists")
        if not processed and not entities and not relations:
            raise ValueError("Extract unit is empty")
        outline = ctx.get("schema_outline")
        expected = set(ctx.get("expected_source_ids") or [])
        if isinstance(outline, dict) and expected:
            draft = {
                "entities": entities,
                "relations": relations,
                "processed_source_ids": [str(item) for item in processed],
            }
            validate_extraction_draft(
                draft,
                outline,
                expected,
                require_complete_sources=False,
            )
        return validator.success(unit_id=unit_id, context={"entity_count": len(entities), "relation_count": len(relations)})
    except (ValueError, ContractError) as exc:
        message = exc.detail.message if isinstance(exc, ContractError) else str(exc)
        err_ctx = dict(ctx)
        if isinstance(exc, ContractError):
            err_ctx.update(exc.detail.context)
        return validator.failure([message], context=err_ctx, unit_id=unit_id)
