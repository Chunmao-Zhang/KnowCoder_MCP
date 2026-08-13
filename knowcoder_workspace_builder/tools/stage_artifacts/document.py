"""Persistence tool exposed only to the Workspace Documenter."""

from __future__ import annotations

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import DocumentWriter


@tool
def save_workspace_readme(
    name: str,
    description: str,
    summary: str,
    incremental_guidance: str,
) -> str:
    """Render the public Workspace README from four prose fields.

    Describe only published Workspace content. Public file references start with
    ontology/, data/, or data/source/. Session, intermediate, attempt, and
    validation paths are private runtime context and do not belong in the prose.
    """
    return DocumentWriter().save(
        name=name,
        description=description,
        summary=summary,
        incremental_guidance=incremental_guidance,
    )
