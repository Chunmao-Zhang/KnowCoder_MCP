"""Persistence tool exposed only to the Data Collector."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import EvidenceWriter


@tool
def save_evidence_manifest(
    coverage: list[dict[str, Any]],
    unresolved_gaps: list[str],
    selected_web_sources: list[dict[str, Any]] | None = None,
) -> str:
    """Write evidence coverage using runtime-owned source IDs and provenance.

    Mark a step covered only when it has no unresolved material limitation. Mark
    it limited or blocked when a limitation remains, and supply exactly one
    consolidated ``Step N: ...`` item in ``unresolved_gaps`` for that step. Copy
    every candidate ID and Chunk ID from successful Fetch output.
    """
    return EvidenceWriter().save(
        coverage=coverage,
        selected_web_sources=list(selected_web_sources or []),
        unresolved_gaps=unresolved_gaps,
    )
