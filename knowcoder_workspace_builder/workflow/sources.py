"""Structured and unstructured source classification values."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError


STRUCTURED_EXTENSIONS = frozenset({".csv", ".tsv", ".xlsx", ".xls", ".xlsm", ".json", ".parquet"})
UNSTRUCTURED_EXTENSIONS = frozenset({".txt", ".md", ".html", ".htm", ".pdf", ".doc", ".docx"})
UNSTRUCTURED_SOURCE_KINDS = frozenset({"web", "web_search", "web_search_bundle", "web_crawl", "model_rewrite"})


def classify_source(path: str | Path) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in STRUCTURED_EXTENSIONS:
        return "structured"
    if suffix in UNSTRUCTURED_EXTENSIONS:
        return "unstructured"
    return "unsupported"


def split_sources(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"structured": [], "unstructured": []}
    for source in sources:
        if not isinstance(source, dict):
            raise ContractError("Every evidence source must be an object")
        kind = str(source.get("source_kind") or "")
        classification = (
            "unstructured"
            if kind in UNSTRUCTURED_SOURCE_KINDS
            else classify_source(str(source.get("file_path") or ""))
        )
        if classification not in result:
            raise ContractError(
                "Evidence source type is unsupported for extraction",
                source_id=source.get("source_id"),
            )
        result[classification].append(source)
    return result
