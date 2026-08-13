"""Persistence tool exposed only to the Structured Data Extractor."""

from __future__ import annotations

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import StructuredExtractionWriter


@tool
def append_instances_batches_from_file() -> str:
    """Merge the runtime-owned structured batch file into the active draft."""
    return StructuredExtractionWriter().save()
