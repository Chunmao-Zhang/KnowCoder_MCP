"""Persistence tool exposed only to the Schema Reviewer."""

from __future__ import annotations

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import SchemaReviewWriter


@tool
def save_schema_judgement(decision: str, missing_requirements: list[str]) -> str:
    """Write the active Schema Reviewer decision to its fixed attempt file."""
    return SchemaReviewWriter().save(decision=decision, missing_requirements=missing_requirements)
