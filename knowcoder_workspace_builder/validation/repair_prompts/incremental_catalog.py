"""Incremental-mode repair cases (per-unit validation during multi-step stages)."""

from __future__ import annotations

from typing import Any

from ._common import contains, contains_all, resolve_cases

# Filename convention: incremental_<stage>
# This module owns per-unit repair hints. After 3 failed unit attempts the unit is skipped.

INCREMENTAL_CASES: dict[str, list] = {
    "evidence": [
        ("step_index", contains("step_index"), "Provide a positive integer step_index for this evidence unit."),
        ("status", contains("status must be"), "Set unit status to covered, limited, or blocked."),
        ("default", lambda e, c: True, "Repair only this step unit, then continue. After 3 failures this unit is skipped."),
    ],
    "schema_build": [
        (
            "entity_as_relation_type",
            contains_all("unsupported type", '"annotation": "entity"'),
            "Use concrete entity class names for relation targets in this unit.",
        ),
        ("unit_fields", contains("requires entities or relations"), "Provide entities or relations for this Schema batch."),
        ("entity_name", contains("entity requires a name"), "Each entity patch item needs a name."),
        (
            "unsupported_field_types",
            contains("unsupported type"),
            "Use supported primitive attribute types and explicit relation endpoints, many, and optional values.",
        ),
        ("default", lambda e, c: True, "Repair only this schema unit/patch. After 3 failures this unit is skipped."),
    ],
    "extract": [
        (
            "entity_names",
            contains("non-empty names", "non-empty name"),
            "Populate the top-level name of every listed entity from its cited source content in one pass.",
        ),
        (
            "entity_id",
            contains("non-empty id", "requires a non-empty id"),
            "Give every entity a stable `id`. Keep the exact entity keys `type`, `id`, `name`, and `attributes`.",
        ),
        (
            "record_shape",
            contains("entity record", "relation record", "attributes", "endpoint", "json-compatible"),
            "Repair every reported canonical Instance field in the same consolidated batch.",
        ),
        (
            "source_refs",
            contains("source_refs", "unassigned source"),
            "Remove model-owned source_refs. Runtime injects provenance for the active unit.",
        ),
        (
            "empty_unit",
            contains("extract unit is empty"),
            (
                "Rebuild the consolidated batch from the source_reader result. "
                "Include every extracted entity and relation. Runtime marks the assigned sources processed."
            ),
        ),
        ("default", lambda e, c: True, "Repair only this extract unit/batch. After 3 failures this unit is skipped and extraction continues."),
    ],
    "structured_extract": [
        ("empty_unit", contains("extract unit is empty", "batches"), "Provide a non-empty structured unit for this chunk."),
        (
            "record_shape",
            contains("entity record", "relation record", "attributes", "endpoint", "json-compatible"),
            "Repair every reported canonical Instance field in this structured chunk.",
        ),
        (
            "source_refs",
            contains("source_refs", "unassigned source"),
            "Remove model-owned source_refs. Runtime injects provenance for the active unit.",
        ),
        ("default", lambda e, c: True, "Repair only this structured chunk. After 3 failures this unit is skipped."),
    ],
}


def resolve(stage: str, *, errors: list[str] | None = None, context: dict[str, Any] | None = None) -> str:
    cases = INCREMENTAL_CASES.get(stage) or INCREMENTAL_CASES["evidence"]
    return resolve_cases(stage, "incremental", cases, errors=errors, context=context)
