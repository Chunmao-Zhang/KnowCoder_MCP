"""Current problem/schema review snapshots and validated user edits."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError, StateConflictError
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.schema import ParsedSchema, parse_schema, schema_from_review
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.transaction import AtomicWriter
from knowcoder_workspace_builder.workflow.models import BuildState


class ReviewService:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.states = BuildStateStore(layout)

    def get(self, session_id: str, review_type: str) -> dict[str, Any]:
        state = self.states.load(session_id)
        self._require_available_review(state, review_type)
        if review_type == "problem":
            problem = state.problem or {}
            steps = problem.get("steps") if isinstance(problem.get("steps"), list) else []
            return {
                "workspace_id": session_id,
                "review_type": "problem",
                "status": "confirmed" if state.problem_confirmed else "draft",
                "question": problem.get("question") or state.question,
                "confirmed_problem": problem.get("question") or state.question,
                "workflow_steps": [
                    {"id": f"step_{index}", "title": str(step), "description": str(step)}
                    for index, step in enumerate(steps, start=1)
                ],
                "version": state.version,
            }
        source = str((state.schema_review or {}).get("schema_source") or "")
        parsed = parse_schema(source, require_relations=False)
        return {
            "workspace_id": session_id,
            "review_type": "schema",
            "status": "confirmed" if state.schema_confirmed else "draft",
            "entities": self._review_entities(parsed),
            "relations": self._review_relations(parsed),
            "schema_source": source,
            "requires_revalidation": bool((state.schema_review or {}).get("requires_revalidation")),
            "version": state.version,
        }

    def save(self, session_id: str, review_type: str, review: dict[str, Any]) -> dict[str, Any]:
        state = self.states.load(session_id)
        self._require_gate(state, review_type)
        supplied_version = review.get("version")
        if supplied_version is not None:
            if not isinstance(supplied_version, int) or isinstance(supplied_version, bool) or supplied_version < 0:
                raise ContractError("Review version must be a non-negative integer")
            if supplied_version != state.version:
                raise StateConflictError(
                    "Review changed after this page was loaded",
                    session_id=session_id,
                    expected_version=supplied_version,
                    current_version=state.version,
                )
        if review_type == "problem":
            question = str(review.get("question") or review.get("confirmed_problem") or "").strip()
            raw_steps = review.get("workflow_steps")
            if not question or not isinstance(raw_steps, list) or not raw_steps:
                raise ContractError("Problem review requires a question and at least one step")
            steps = [str(item.get("description") or item.get("title") or "").strip() for item in raw_steps if isinstance(item, dict)]
            if len(steps) != len(raw_steps) or any(not item for item in steps):
                raise ContractError("Every problem review step must be non-empty")
            updated = self.states.update(
                session_id,
                state.version,
                lambda current: self._save_problem(current, question, steps),
            )
            normalized = self._problem_review(updated, question, steps, review)
            self._write_snapshot(updated, "problem", normalized)
            return {
                "ok": True,
                "status": "draft",
                "requires_revalidation": False,
                "version": updated.version,
                "review": normalized,
            }

        normalized = self._normalize_schema_review(review)
        candidate_source = schema_from_review(normalized, require_relations=False)
        candidate_parsed = parse_schema(candidate_source, require_relations=False)
        current_source = str((state.schema_review or {}).get("schema_source") or "")
        current_parsed = parse_schema(current_source, require_relations=False)
        changed = candidate_parsed.outline() != current_parsed.outline()
        source = candidate_source if changed else current_source
        parsed = candidate_parsed if changed else current_parsed
        updated = self.states.update(
            session_id,
            state.version,
            lambda current: self._save_schema(current, source, parsed, changed),
        )
        self._write_snapshot(updated, "schema", normalized)
        return {
            "ok": True,
            "status": "draft",
            "requires_revalidation": changed,
            "version": updated.version,
            "schema_source": source,
            "form": self.form_from_schema(parsed),
        }

    @staticmethod
    def _problem_review(
        state: BuildState,
        question: str,
        steps: list[str],
        submitted: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "workspace_id": state.session_id,
            "review_type": "problem",
            "status": "draft",
            "question": question,
            "confirmed_problem": question,
            "workflow_steps": [
                {"id": f"step_{index}", "title": step, "description": step}
                for index, step in enumerate(steps, start=1)
            ],
            "revision_instruction": str(submitted.get("revision_instruction") or ""),
            "version": state.version,
        }

    def save_schema_form(self, session_id: str, form: list[dict[str, Any]]) -> dict[str, Any]:
        entities: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for item in form:
            if not isinstance(item, dict):
                raise ContractError("Schema form rows must be objects")
            if item.get("type") == "entity":
                attributes = item.get("attributes") or []
                if not isinstance(attributes, list) or any(not isinstance(field, dict) for field in attributes):
                    raise ContractError("Schema entity attributes must be objects")
                entities.append(
                    {
                        "name": item.get("name") or item.get("entity_type"),
                        "id_type": item.get("id_type") or item.get("entity_data_type"),
                        "description": item.get("description") or "",
                        "attributes": [
                            {
                                "name": field.get("name") or field.get("attribute"),
                                "type": field.get("type") or field.get("attribute_data_type"),
                                "optional": bool(field.get("optional")),
                            }
                            for field in attributes
                        ],
                    }
                )
            elif item.get("type") == "relation":
                relations.append(
                    {
                        "name": item.get("name") or item.get("relation_type"),
                        "head": item.get("head") or item.get("head_entity_type"),
                        "tail": item.get("tail") or item.get("tail_entity_type"),
                        "description": item.get("description") or "",
                        "many": bool(item.get("many")),
                        "optional": bool(item.get("optional")),
                        "directed": True,
                    }
                )
            else:
                raise ContractError("Schema form row has an unknown type")
        result = self.save(session_id, "schema", {"entities": entities, "relations": relations})
        return {**result, "status": "draft", "schema_text": result["schema_source"], "run_id": session_id}

    @staticmethod
    def form_from_schema(parsed: ParsedSchema) -> list[dict[str, Any]]:
        form: list[dict[str, Any]] = []
        for entity in parsed.entities:
            form.append(
                {
                    "type": "entity",
                    "name": entity.name,
                    "entity_type": entity.name,
                    "id_type": entity.id_type,
                    "entity_data_type": entity.id_type,
                    "description": entity.description,
                    "attributes": [
                        {
                            "name": field.name,
                            "attribute": field.name,
                            "type": field.value_type,
                            "attribute_data_type": field.value_type,
                            "optional": field.optional,
                        }
                        for field in entity.attributes
                        if field.name != "name"
                    ],
                }
            )
            for relation in entity.relations:
                form.append(
                    {
                        "type": "relation",
                        "name": relation.name,
                        "relation_type": relation.name,
                        "head": entity.name,
                        "head_entity_type": entity.name,
                        "tail": relation.value_type,
                        "tail_entity_type": relation.value_type,
                        "description": relation.description,
                        "many": relation.many,
                        "optional": relation.optional,
                        "directed": True,
                    }
                )
        return form

    @staticmethod
    def _review_entities(parsed: ParsedSchema) -> list[dict[str, Any]]:
        return [
            {
                "name": entity.name,
                "id_type": entity.id_type,
                "description": entity.description,
                "fields": [
                    {"name": field.name, "type": field.value_type, "optional": field.optional}
                    for field in entity.attributes
                    if field.name != "name"
                ],
            }
            for entity in parsed.entities
        ]

    @staticmethod
    def _review_relations(parsed: ParsedSchema) -> list[dict[str, Any]]:
        return [
            {
                "name": relation.name,
                "head": entity.name,
                "tail": relation.value_type,
                "description": relation.description,
                "many": relation.many,
                "optional": relation.optional,
                "directed": True,
            }
            for entity in parsed.entities
            for relation in entity.relations
        ]

    @staticmethod
    def _normalize_schema_review(review: dict[str, Any]) -> dict[str, Any]:
        entities = review.get("entities")
        relations = review.get("relations")
        if not isinstance(entities, list) or not isinstance(relations, list):
            raise ContractError("Schema review requires entity and relation lists")
        if any(not isinstance(item, dict) for item in entities):
            raise ContractError("Every schema review entity must be an object")
        if any(not isinstance(item, dict) for item in relations):
            raise ContractError("Every schema review relation must be an object")
        if any(item.get("directed") is False for item in relations):
            raise ContractError("Schema relations are directed and require explicit head and tail entities")
        return {
            "entities": [
                {
                    **item,
                    "attributes": item.get("attributes") if isinstance(item.get("attributes"), list) else item.get("fields", []),
                }
                for item in entities
            ],
            "relations": [dict(item) for item in relations],
        }

    @staticmethod
    def _require_gate(state: BuildState, review_type: str) -> None:
        expected = "needs_problem_confirmation" if review_type == "problem" else "needs_schema_confirmation"
        if review_type not in {"problem", "schema"} or state.status != expected:
            raise StateConflictError("Review type does not match the current Builder gate", status=state.status)

    @staticmethod
    def _require_available_review(state: BuildState, review_type: str) -> None:
        if review_type not in {"problem", "schema"}:
            raise ContractError("Review type must be problem or schema", review_type=review_type)
        if review_type == "problem" and not isinstance(state.problem, dict):
            raise StateConflictError("Problem review is not available for this Session", status=state.status)
        schema_source = str((state.schema_review or {}).get("schema_source") or "")
        if review_type == "schema" and not schema_source.strip():
            raise StateConflictError("Schema review is not available for this Session", status=state.status)

    @staticmethod
    def _save_problem(current: BuildState, question: str, steps: list[str]) -> BuildState:
        current.question = question
        current.problem = {
            **dict(current.problem or {}),
            "question": question,
            "scope": {"question": question, "steps": list(steps)},
            "steps": steps,
        }
        current.problem_confirmed = False
        return current

    @staticmethod
    def _save_schema(current: BuildState, source: str, parsed: ParsedSchema, changed: bool) -> BuildState:
        current.schema_review = {
            **dict(current.schema_review or {}),
            "schema_source": source,
            "schema_outline": parsed.outline(),
            "requires_revalidation": changed,
        }
        current.schema_confirmed = False
        return current

    def _write_snapshot(self, state: BuildState, review_type: str, review: dict[str, Any]) -> None:
        paths = self.layout.session(state.session_id)
        AtomicWriter(paths).json(paths.research / f"current_{review_type}_review.json", review)
