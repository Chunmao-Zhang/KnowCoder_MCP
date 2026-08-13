"""Persistence tool exposed only to the Unstructured Data Extractor."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import UnstructuredExtractionWriter


@tool
def append_instances_batch(
    entities: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> str:
    """Merge one semantic Instance batch into the active unstructured draft."""
    return UnstructuredExtractionWriter().save(entities=entities, relations=relations)
