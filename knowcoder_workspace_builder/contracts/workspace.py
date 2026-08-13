"""Public executable knowledge Workspace contract."""

from __future__ import annotations

from typing import Final


PUBLIC_WORKSPACE_FILES: Final[tuple[str, ...]] = (
    "README.md",
    "workspace.yaml",
    "ontology/README.md",
    "ontology/types.py",
    "ontology/loader.py",
    "ontology/schema.json",
    "data/entities.jsonl",
    "data/relations.jsonl",
    "data/source_chunks.jsonl",
    "data/manifest.json",
)

PUBLIC_WORKSPACE_DIRECTORIES: Final[tuple[str, ...]] = (
    "ontology",
    "knowledge",
    "data",
    "data/source",
)

SCHEMA_PRIMITIVES: Final[frozenset[str]] = frozenset({"str", "int", "float", "bool"})
SCHEMA_ID_TYPES: Final[frozenset[str]] = frozenset({"str", "int"})
