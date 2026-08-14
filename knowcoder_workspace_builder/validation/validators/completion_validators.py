"""Final stage-completion validators."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.validation.extraction import validate_extraction_draft
from knowcoder_workspace_builder.validation.validators.base import (
    BaseValidator,
    ValidationMode,
    ValidationOutcome,
)


class ProblemCompletionValidator(BaseValidator):
    stage = "problem"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        ctx = dict(context or {})
        try:
            data = self.require_mapping(payload, label="problem handoff")
            action = str(data.get("workspace_action") or "")
            base_workspace_id = str(data.get("base_workspace_id") or "").strip()
            if action not in {"new", "extend"}:
                raise ValueError("Problem handoff workspace_action must be new or extend")
            workspace_context = ctx.get("workspace_context")
            catalog = workspace_context.get("workspace_catalog") if isinstance(workspace_context, dict) else []
            catalog_ids = {
                str(item.get("workspace_id") or "")
                for item in (catalog or [])
                if isinstance(item, dict) and str(item.get("workspace_id") or "").strip()
            }
            if action == "new" and base_workspace_id:
                raise ValueError("New Workspace selection requires an empty base_workspace_id")
            if action == "extend" and base_workspace_id not in catalog_ids:
                raise ValueError("Extension selection requires a listed base_workspace_id")
            required_base_workspace_id = (
                str(workspace_context.get("required_base_workspace_id") or "").strip()
                if isinstance(workspace_context, dict)
                else ""
            )
            if required_base_workspace_id and (
                action != "extend" or base_workspace_id != required_base_workspace_id
            ):
                raise ValueError(
                    "In-place extension must use the current Session baseline: "
                    + required_base_workspace_id
                )
            if not str(data.get("question") or "").strip():
                raise ValueError("Problem handoff question cannot be empty")
            if not isinstance(data.get("scope"), dict):
                raise ValueError("Problem handoff scope must be an object")
            self.text_list(data.get("steps"), field="steps", allow_empty=False)
            self.text_list(data.get("missing_information"), field="missing_information")
            return self.success()
        except ValueError as exc:
            return self.failure([str(exc)], context=ctx)


class EvidenceCompletionValidator(BaseValidator):
    stage = "evidence"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        ctx = dict(context or {})
        try:
            data = self.require_mapping(payload, label="evidence handoff")
            for field in ("coverage", "sources", "unresolved_gaps", "blocking_gaps"):
                if not isinstance(data.get(field), list):
                    raise ValueError(f"Evidence handoff {field} must be a list")
            self.text_list(data.get("unresolved_gaps"), field="unresolved_gaps")
            blocking = self.text_list(data.get("blocking_gaps"), field="blocking_gaps")
            source_ids = []
            for source in data["sources"]:
                if not isinstance(source, dict) or not str(source.get("source_id") or "").strip():
                    raise ValueError("Every evidence source requires a source_id")
                source_ids.append(str(source["source_id"]))
            if not source_ids:
                raise ValueError("Evidence completion requires at least one formal source")
            if len(source_ids) != len(set(source_ids)):
                raise ValueError("Evidence source IDs must be unique")
            workspace_context = ctx.get("workspace_context")
            required_source_ids = self.text_list(
                workspace_context.get("required_source_ids") or [],
                field="required_source_ids",
            ) if isinstance(workspace_context, dict) else []
            missing_required = sorted(set(required_source_ids) - set(source_ids))
            if missing_required:
                raise ValueError(f"Evidence sources omit required registered sources: {missing_required}")
            steps = list(ctx.get("steps") or [])
            if steps and len(data["coverage"]) != len(steps):
                raise ValueError("Evidence coverage must contain one item for every confirmed step")
            bound_source_ids: set[str] = set()
            covered_step_indexes: set[int] = set()
            for index, item in enumerate(data["coverage"]):
                if not isinstance(item, dict):
                    raise ValueError(f"Every evidence coverage item must be an object at position {index + 1}")
                step_index = item.get("step_index")
                if step_index is None and steps:
                    step_text = str(item.get("step") or "").strip()
                    matching_indexes = [
                        candidate_index
                        for candidate_index, confirmed_step in enumerate(steps, start=1)
                        if step_text == str(confirmed_step).strip()
                    ]
                    if len(matching_indexes) == 1:
                        step_index = matching_indexes[0]
                if not isinstance(step_index, int) or isinstance(step_index, bool):
                    raise ValueError("Every evidence coverage item requires an integer step_index")
                if step_index in covered_step_indexes:
                    raise ValueError(f"Evidence coverage contains duplicate step_index {step_index}")
                if steps and not 1 <= step_index <= len(steps):
                    raise ValueError(f"Evidence coverage step_index is outside the confirmed range: {step_index}")
                if steps and str(item.get("step") or "").strip() != str(steps[step_index - 1]):
                    raise ValueError(f"Evidence coverage step does not match confirmed step {step_index}")
                covered_step_indexes.add(step_index)
                self.text_list(item.get("requirements"), field="requirements", allow_empty=False)
                status = item.get("status")
                if status not in {"covered", "limited", "blocked"}:
                    raise ValueError(f"Evidence coverage status is invalid at position {index + 1}")
                covered_by = self.text_list(item.get("source_ids"), field="source_ids")
                bound_source_ids.update(covered_by)
                if status == "covered" and not covered_by:
                    raise ValueError(f"Covered evidence requires at least one source ID at position {index + 1}")
                unknown = sorted(set(covered_by) - set(source_ids))
                if unknown:
                    raise ValueError(f"Evidence coverage references unknown sources: {unknown}")
                chunk_refs = item.get("chunk_refs", [])
                if not isinstance(chunk_refs, list):
                    raise ValueError("Evidence coverage chunk_refs must be a list")
                for ref in chunk_refs:
                    if not isinstance(ref, dict):
                        raise ValueError("Evidence chunk references must be objects")
                    source_id = str(ref.get("source_id") or "").strip()
                    chunk_id = str(ref.get("chunk_id") or "").strip()
                    if not source_id or not chunk_id:
                        raise ValueError("Evidence chunk references require source_id and chunk_id")
                    if source_id not in covered_by:
                        raise ValueError("Evidence chunk reference belongs to an unbound source")
            unbound_required = sorted(set(required_source_ids) - bound_source_ids)
            if unbound_required:
                raise ValueError(f"Evidence coverage does not bind required sources to a confirmed step: {unbound_required}")
            if steps and covered_step_indexes != set(range(1, len(steps) + 1)):
                raise ValueError("Evidence coverage does not cover every confirmed step_index")
            limitations = list(data.get("unresolved_gaps") or [])
            unresolved_step_count = sum(
                1
                for item in data["coverage"]
                if isinstance(item, dict) and item.get("status") in {"limited", "blocked"}
            )
            if len(limitations) != unresolved_step_count:
                raise ValueError(
                    "Evidence limitations must contain one consolidated item for each limited or blocked step"
                )
            limitations.extend(item for item in blocking if item not in limitations)
            return self.success(context={"limitations": limitations})
        except ValueError as exc:
            return self.failure([str(exc)], context=ctx)


class SchemaBuildCompletionValidator(BaseValidator):
    stage = "schema_build"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        try:
            from knowcoder_workspace_builder.storage.schema import parse_schema

            data = self.require_mapping(payload, label="schema handoff")
            source = str(data.get("schema_source") or "").strip()
            if not source:
                raise ValueError("Schema artifact requires runtime-compiled schema_source")
            parsed = parse_schema(source, require_relations=False)
            return self.success(
                context={
                    "entity_count": len(parsed.entity_names),
                    "relation_count": len(parsed.relation_names),
                }
            )
        except (ValueError, ContractError) as exc:
            message = exc.detail.message if isinstance(exc, ContractError) else str(exc)
            ctx = dict(context or {})
            if isinstance(exc, ContractError):
                ctx.update(exc.detail.context)
            return self.failure([message], context=ctx)


class SchemaJudgeCompletionValidator(BaseValidator):
    """Light judge: decision + optional missing_requirements only."""

    stage = "schema_judge"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        try:
            data = self.require_mapping(payload, label="schema judgement")
            decision = data.get("decision")
            if decision not in {"pass", "revise"}:
                raise ValueError("Schema judgement decision must be pass or revise")
            missing = self.text_list(data.get("missing_requirements"), field="missing_requirements")
            if decision == "pass" and missing:
                raise ValueError("Passing schema judgement cannot contain missing_requirements")
            if decision == "revise" and not missing:
                raise ValueError("Schema revision requires at least one missing requirement")
            return self.success()
        except ValueError as exc:
            return self.failure([str(exc)], context=context)


class ExtractCompletionValidator(BaseValidator):
    stage = "extract"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        return _validate_extract_like(self, payload, context=context, stage="extract")


class StructuredExtractCompletionValidator(BaseValidator):
    stage = "structured_extract"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        return _validate_extract_like(self, payload, context=context, stage="structured_extract")


class WorkspaceDocumentCompletionValidator(BaseValidator):
    stage = "document"
    mode = ValidationMode.COMPLETION

    def validate(self, payload: Any, *, context: dict[str, Any] | None = None) -> ValidationOutcome:
        try:
            data = self.require_mapping(payload, label="Workspace documentation")
            fields = ("name", "description")
            missing = [field for field in fields if not str(data.get(field) or "").strip()]
            if missing:
                raise ValueError(f"Workspace documentation fields require non-empty text: {missing}")
            return self.success()
        except ValueError as exc:
            return self.failure([str(exc)], context=dict(context or {}))


def _validate_extract_like(
    validator: BaseValidator,
    payload: Any,
    *,
    context: dict[str, Any] | None,
    stage: str,
) -> ValidationOutcome:
    ctx = dict(context or {})
    try:
        data = validator.require_mapping(payload, label=f"{stage} handoff")
        status = str(ctx.get("status") or "completed")
        if status == "skipped":
            if ctx.get("sources"):
                raise ValueError("Extraction can be skipped only when its source list is empty")
            return validator.success()
        processed = validator.text_list(data.get("processed_source_ids"), field="processed_source_ids")
        if len(processed) != len(set(processed)):
            raise ValueError("processed_source_ids must be unique")
        for field in ("entity_count", "relation_count"):
            count = data.get(field)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        sources = ctx.get("sources") or []
        expected: set[str] = set()
        if isinstance(sources, list):
            expected = {str(item.get("source_id") or "") for item in sources if isinstance(item, dict)}
            expected.discard("")
            if expected and set(processed) != expected:
                raise ValueError(
                    "Extraction completion must cover every assigned source; "
                    f"missing={sorted(expected - set(processed))}; unexpected={sorted(set(processed) - expected)}"
                )
        draft = ctx.get("draft")
        outline = ctx.get("schema_outline")
        if isinstance(draft, dict) and isinstance(outline, dict) and expected:
            validate_extraction_draft(
                draft,
                outline,
                expected,
                require_complete_sources=True,
            )
            if len(draft.get("entities") or []) != data.get("entity_count"):
                raise ValueError("Extractor entity count does not match the stored draft")
            if len(draft.get("relations") or []) != data.get("relation_count"):
                raise ValueError("Extractor relation count does not match the stored draft")
        return validator.success()
    except (ValueError, ContractError) as exc:
        message = exc.detail.message if isinstance(exc, ContractError) else str(exc)
        err_ctx = dict(ctx)
        if isinstance(exc, ContractError):
            err_ctx.update(exc.detail.context)
        return validator.failure([message], context=err_ctx)
