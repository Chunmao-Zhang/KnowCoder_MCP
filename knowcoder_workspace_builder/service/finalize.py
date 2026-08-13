"""Merge accepted extraction drafts and commit the current Workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.storage.instances import validate_instances
from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.sources import SourceRepository
from knowcoder_workspace_builder.storage.stage_artifacts import merge_final_drafts
from knowcoder_workspace_builder.storage.transaction import read_json
from knowcoder_workspace_builder.storage.workspace import WorkspaceRepository


def _complete_referenced_sources(
    paths: SessionPaths,
    sources: list[dict[str, Any]],
    instances: dict[str, Any],
) -> list[dict[str, Any]]:
    """Add authoritative baseline source records still referenced by merged facts."""
    resolved: list[dict[str, Any]] = []
    resolved_ids: set[str] = set()
    for record in sources:
        source_id = str(record.get("source_id") or "").strip()
        if source_id and source_id not in resolved_ids:
            resolved_ids.add(source_id)
            resolved.append(dict(record))

    referenced_ids = {
        str(source_id).strip()
        for record in [*instances.get("entities", []), *instances.get("relations", [])]
        if isinstance(record, dict)
        for source_id in (record.get("source_refs") or [])
        if str(source_id).strip()
    }
    missing_ids = referenced_ids - resolved_ids
    if not missing_ids:
        return resolved

    stored_records = {
        str(record.get("source_id") or "").strip(): record
        for record in SourceRepository(paths).list()
        if str(record.get("source_id") or "").strip()
    }
    unresolved = sorted(missing_ids - stored_records.keys())
    if unresolved:
        raise ContractError(
            "Merged instances reference sources missing from the Session source repository",
            source_ids=unresolved,
        )
    for source_id in sorted(missing_ids):
        resolved.append(dict(stored_records[source_id]))
    return resolved


def finalize_workspace(
    *,
    paths: SessionPaths,
    schema_source: str,
    draft_paths: list[Path],
    sources: list[dict[str, Any]],
    schema_version: int,
    data_version: int,
    readme: str,
) -> dict[str, Any]:
    drafts = [read_json(path) for path in draft_paths]
    instances = merge_final_drafts(drafts)
    schema = parse_schema(schema_source, require_relations=False)
    validate_instances(instances, schema)
    complete_sources = _complete_referenced_sources(paths, sources, instances)
    files = WorkspaceRepository(paths).commit(
        schema_source=schema_source,
        instances=instances,
        sources=complete_sources,
        schema_version=schema_version,
        data_version=data_version,
        readme=readme,
    )
    return {
        "files": files,
        "entity_count": len(instances["entities"]),
        "relation_count": len(instances["relations"]),
    }
