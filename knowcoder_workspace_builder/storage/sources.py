"""Session-owned source manifest persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .locks import SessionLockStore
from .paths import ProjectLayout, SessionPaths
from .transaction import AtomicWriter, read_json


SOURCE_CATEGORIES = frozenset({"user_uploads", "web_search", "web_crawls", "model_rewrites"})


class SourceRepository:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self.manifest_path = paths.research / "source_manifest.json"
        self.locks = SessionLockStore(ProjectLayout(paths.project))

    def list(self) -> list[dict[str, Any]]:
        if not self.manifest_path.is_file():
            return []
        value = read_json(self.manifest_path)
        if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
            raise ContractError("Source manifest is invalid", path=str(self.manifest_path))
        return [dict(item) for item in value["sources"] if isinstance(item, dict)]

    def register(self, category: str, record: dict[str, Any]) -> dict[str, Any]:
        if category not in SOURCE_CATEGORIES:
            raise ContractError("Unknown source category", category=category)
        source_id = str(record.get("source_id") or "").strip()
        if not source_id:
            raise ContractError("Source record requires a source_id")
        normalized = {**record, "source_id": source_id, "category": category}
        with self.locks.acquire(self.paths.session_id):
            records = self.list()
            existing = next((item for item in records if str(item.get("source_id")) == source_id), None)
            if existing is not None and existing != normalized:
                raise ContractError("Source ID already refers to different evidence", source_id=source_id)
            if existing is None:
                records.append(normalized)
                AtomicWriter(self.paths).json(
                    self.manifest_path,
                    {"format_version": 1, "sources": records},
                )
        return normalized

    def register_version(
        self,
        category: str,
        record: dict[str, Any],
        *,
        supersedes: list[str],
    ) -> dict[str, Any]:
        """Register a new source version and retain replaced records for provenance."""
        if category not in SOURCE_CATEGORIES:
            raise ContractError("Unknown source category", category=category)
        source_id = str(record.get("source_id") or "").strip()
        previous_ids = {str(item).strip() for item in supersedes if str(item).strip()}
        if not source_id:
            raise ContractError("Source record requires a source_id")
        if source_id in previous_ids:
            raise ContractError("A source version cannot supersede itself", source_id=source_id)
        normalized = {**record, "source_id": source_id, "category": category, "status": "active"}
        with self.locks.acquire(self.paths.session_id):
            records = self.list()
            known_ids = {str(item.get("source_id") or "") for item in records}
            missing = sorted(previous_ids - known_ids)
            if missing:
                raise ContractError("Superseded source records were not found", source_ids=missing)
            existing = next((item for item in records if str(item.get("source_id")) == source_id), None)
            if existing is not None and existing != normalized:
                raise ContractError("Source ID already refers to different evidence", source_id=source_id)
            superseded_at = datetime.now(UTC).isoformat()
            updated = [
                {
                    **item,
                    "status": "superseded",
                    "superseded_by": source_id,
                    "superseded_at": superseded_at,
                }
                if str(item.get("source_id") or "") in previous_ids
                else item
                for item in records
            ]
            if existing is None:
                updated.append(normalized)
            AtomicWriter(self.paths).json(
                self.manifest_path,
                {"format_version": 1, "sources": updated},
            )
        return normalized
