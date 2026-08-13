"""Build the current Workspace source and data quality audit."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .schema import ParsedSchema


def build_audit(
    instances: dict[str, Any],
    schema: ParsedSchema,
    sources: list[dict[str, Any]],
    *,
    schema_version: int,
    data_version: int,
) -> dict[str, Any]:
    entities = instances.get("entities") or []
    relations = instances.get("relations") or []
    source_statuses = Counter(str(item.get("status") or "unknown") for item in sources if isinstance(item, dict))
    entity_types = Counter(str(item.get("type") or "") for item in entities if isinstance(item, dict))
    relation_types = Counter(str(item.get("type") or "") for item in relations if isinstance(item, dict))
    return {
        "format_version": 1,
        "schema_version": schema_version,
        "data_version": data_version,
        "sources": {
            "total": len(sources),
            "by_status": dict(sorted(source_statuses.items())),
            "records": sources,
        },
        "records": {
            "raw": sum(int(item.get("raw_records") or 0) for item in sources if isinstance(item, dict)),
            "extracted_entities": len(entities),
            "extracted_relations": len(relations),
            "deduplicated_entities": len(entities),
            "deduplicated_relations": len(relations),
        },
        "entity_types": dict(sorted(entity_types.items())),
        "relation_types": dict(sorted(relation_types.items())),
        "schema": schema.outline(),
        "missing_fields": [],
        "unresolved_relations": [],
    }
