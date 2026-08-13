"""Schema Engineer candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.runtime.candidate_normalization import (
    merge_schema_blueprint,
    schema_blueprint_from_source,
)
from knowcoder_workspace_builder.storage.schema import compile_schema_payload, parse_schema
from knowcoder_workspace_builder.storage.stage_artifacts import artifact_path
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json

from .base import BaseStageWriter


class SchemaWriter(BaseStageWriter):
    stage = "schema_build"
    tool_name = "save_schema"
    error_type = "invalid_schema"

    def save(
        self,
        *,
        entities: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        remove_entity_names: list[str],
        remove_relation_names: list[str],
    ) -> str:
        def operation() -> dict[str, Any]:
            context = self.context()
            paths = self.paths()
            blueprint_path = artifact_path(paths, context.attempt_id, "schema_blueprint")
            if blueprint_path.is_file():
                current = read_json(blueprint_path)
                if not isinstance(current, dict):
                    raise ContractError("Saved schema blueprint must be an object")
            else:
                workspace_context = context.input.get("workspace_context")
                current_source = (
                    workspace_context.get("current_schema")
                    if isinstance(workspace_context, dict)
                    else None
                )
                current = (
                    schema_blueprint_from_source(str(current_source))
                    if str(current_source or "").strip()
                    else {"entities": [], "relations": []}
                )
            blueprint, changes = merge_schema_blueprint(
                current,
                entities=entities,
                relations=relations,
                remove_entity_names=remove_entity_names,
                remove_relation_names=remove_relation_names,
            )
            source = compile_schema_payload(blueprint, require_relations=False)
            parsed = parse_schema(source, require_relations=False)
            AtomicWriter(paths).json(blueprint_path, blueprint)
            target = self.persist("schema_draft", source, suffix=".py")
            changes.extend(
                [
                    {"field": "schema_draft.py", "action": "derived", "detail": "Compiled from the semantic blueprint."},
                    {"field": "Entity", "action": "derived", "detail": "Generated the shared identity base class."},
                    {"field": "typing imports", "action": "derived", "detail": "Generated from field cardinality and optionality."},
                ]
            )
            return {
                "candidate_path": self.virtual(target),
                "normalization_log": self.normalization_log(changes),
                "entity_count": len(parsed.entities),
                "relation_count": len(parsed.relation_names),
            }

        return self.execute(operation)
