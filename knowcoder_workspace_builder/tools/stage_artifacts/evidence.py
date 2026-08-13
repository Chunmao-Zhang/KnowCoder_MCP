"""Persistence tool exposed only to the Data Collector."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import EvidenceWriter


@tool
def save_evidence_manifest(
    coverage: list[dict[str, Any]],
    unresolved_gaps: list[str],
) -> str:
    """Write evidence coverage using runtime-owned source IDs and provenance."""
    return EvidenceWriter().save(coverage=coverage, unresolved_gaps=unresolved_gaps)
