"""Persistence tool exposed only to the Schema Engineer."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import SchemaWriter


@tool
def save_schema(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    remove_entity_names: list[str] | None = None,
    remove_relation_names: list[str] | None = None,
) -> str:
    """Incrementally upsert the semantic Schema and compile its Python artifact."""
    return SchemaWriter().save(
        entities=entities,
        relations=relations,
        remove_entity_names=list(remove_entity_names or []),
        remove_relation_names=list(remove_relation_names or []),
    )
