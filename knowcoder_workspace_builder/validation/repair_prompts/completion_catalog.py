"""Completion-mode repair cases (final Subagent handoff validation)."""

from __future__ import annotations

from typing import Any

from ._common import contains, contains_all, resolve_cases

# Filename convention: completion_<stage>
# This module owns final handoff repair hints for each stage.

COMPLETION_CASES: dict[str, list] = {
    "problem": [
        (
            "missing_artifact",
            contains("artifact does not exist", "problem_review"),
            "Call save_problem_review with the complete problem plan. Validation only inspects the saved file.",
        ),
        ("scope_object", contains("scope must be an object"), "Set scope to an object."),
        ("steps_required", contains("steps"), "Provide ordered unique non-empty steps."),
        ("default", lambda e, c: True, "Persist the complete problem plan with save_problem_review, then wait for file validation."),
    ],
    "evidence": [
        (
            "missing_artifact",
            contains("artifact does not exist", "evidence_manifest"),
            (
                "Complete missing searches for uncovered steps, then call save_evidence_manifest successfully. "
                "Validation only inspects the saved evidence_manifest.json file."
            ),
        ),
        (
            "missing_first_pass_search",
            contains("first-pass search"),
            "Run web_search for each missing_step_indexes value. Use the same one-based step position.",
        ),
        ("no_sources", contains("at least one source"), "Call web_search successfully before completion."),
        ("covered_without_source", contains("covered evidence requires runtime-bound source evidence"), "Run web_search before marking the step covered."),
        ("coverage_length", contains("missing steps"), "Return one coverage item per uncovered step using its one-based step_index."),
        (
            "required_sources",
            contains("required registered sources", "bind required sources"),
            (
                "Preserve accepted coverage and run needed searches. Runtime binds required uploads and registered source IDs."
            ),
        ),
        ("default", lambda e, c: True, "Repair coverage in the saved manifest through save_evidence_manifest."),
    ],
    "schema_build": [
        (
            "missing_artifact",
            contains("artifact does not exist", "schema_draft"),
            "Call save_schema with semantic entities and relations. Runtime compiles schema_draft.py.",
        ),
        (
            "missing_outline",
            contains("requires entities/relations", "entities/relations outline"),
            "Provide complete semantic entity and relation objects to save_schema.",
        ),
        (
            "top_level",
            contains("only imports and class declarations"),
            "Repair the semantic entity or relation data. Runtime generates the Python module.",
        ),
        (
            "entity_base",
            contains("must define class entity", "class entity must declare"),
            "Provide entity id_type values. Runtime generates the Entity identity base class.",
        ),
        (
            "unsupported_field_types/entity_as_relation_type",
            contains_all("unsupported type", '"annotation": "entity"'),
            "Repair every reported semantic relation endpoint in one pass.",
        ),
        (
            "unsupported_field_types",
            contains("unsupported type"),
            (
                "Repair every reported semantic field in one pass. Use primitive attribute types and explicit many and optional relation flags."
            ),
        ),
        ("relation_unique", contains("relation field names must be unique"), "Use unique owner-prefixed semantic relation names."),
        ("entity_description", contains("non-empty description", "names and descriptions"), "Give every entity a non-empty description."),
        ("relation_description", contains("relation requires a non-empty description"), "Give every relation a non-empty description."),
        ("default", lambda e, c: True, "Call save_schema again with corrected semantic entities and relations. Runtime validates the fixed file."),
    ],
    "schema_judge": [
        (
            "missing_artifact",
            contains("artifact does not exist", "schema_judgement"),
            "Call save_schema_judgement with decision and missing_requirements. Validation only inspects the saved file.",
        ),
        ("decision_value", contains("decision"), "Set decision to pass or revise."),
        ("revise_requires_missing", contains("requires at least one missing requirement"), "For revise, provide concrete missing_requirements."),
        ("pass_no_missing", contains("cannot contain missing_requirements"), "For pass, missing_requirements must be empty."),
        ("default", lambda e, c: True, "Persist only decision and missing_requirements with save_schema_judgement."),
    ],
    "extract": [
        (
            "missing_artifact",
            contains("artifact does not exist", "unstructured_draft"),
            (
                "Call append_instances_batch successfully for the current unit. "
                "Validation only inspects the saved draft file."
            ),
        ),
        (
            "entity_names",
            contains("non-empty names", "non-empty name"),
            "Populate the top-level name of every listed entity from its cited source content in one pass.",
        ),
        (
            "missing_sources",
            contains("cover every assigned source", "process every assigned source"),
            "Finish remaining extraction units. Append semantic empty record lists when an assigned source has no extractable fact.",
        ),
        (
            "source_refs",
            contains("source_refs", "unassigned source"),
            "Remove model-owned provenance fields. Runtime injects source_refs from the assigned unit.",
        ),
        (
            "entity_id",
            contains("non-empty id", "requires a non-empty id"),
            "Give every entity a stable `id` and keep its exact `type`, `name`, and `attributes` keys.",
        ),
        (
            "attributes",
            contains("attributes must be an object", "json-compatible"),
            "Use a JSON-compatible object for every entity and relation attributes value.",
        ),
        (
            "relation_endpoint",
            contains("relation endpoints", "missing entity", "head.type", "tail.type"),
            "Use head and tail objects with type and id, and include both endpoint entities in the saved draft.",
        ),
        ("default", lambda e, c: True, "Repair the canonical Instance fields and persist the unit with append_instances_batch."),
    ],
    "structured_extract": [
        (
            "missing_artifact",
            contains("missing its stage artifact", "structured_draft", "artifact does not exist"),
            (
                "Write the fixed runtime batch file. Use head and tail endpoint objects with type and id for relations. "
                "Call append_instances_batches_from_file. Finish after it returns ok=true. Validation only inspects the saved draft."
            ),
        ),
        (
            "missing_sources",
            contains("cover every assigned source"),
            "Evaluate each missing source. Runtime derives processed_source_ids, including sources with no extractable row.",
        ),
        ("batch_structure", contains("batches"), "Keep the fixed batch file structure with a batches list."),
        (
            "record_shape",
            contains("entity record", "relation record", "attributes", "endpoint"),
            "Repair the canonical entity or relation fields in every invalid row.",
        ),
        ("default", lambda e, c: True, "Repair only invalid Instance rows and persist the canonical batch again."),
    ],
    "document": [
        (
            "required_fields",
            contains("documentation fields", "model content is incomplete"),
            "Provide non-empty name, description, summary, and incremental_guidance.",
        ),
        (
            "public_paths",
            contains("non-public paths", "private session", "workspace-relative paths", "path separators"),
            (
                "Remove every path listed in invalid_paths. Describe only published Workspace files under "
                "ontology/, data/, and data/source/. Keep Session, intermediate, attempts, and validation paths out of README content. "
                "Call save_workspace_readme with the corrected complete fields."
            ),
        ),
        ("default", lambda e, c: True, "Return the complete Workspace documentation JSON object."),
    ],
}


def resolve(stage: str, *, errors: list[str] | None = None, context: dict[str, Any] | None = None) -> str:
    cases = COMPLETION_CASES.get(stage) or COMPLETION_CASES["problem"]
    return resolve_cases(stage, "completion", cases, errors=errors, context=context)
