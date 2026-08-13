"""Project Workspace catalog and immutable-baseline extension support."""

from __future__ import annotations

import json
import shutil
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError, StateConflictError

from .canonical import clone_stage_files
from .paths import ProjectLayout
from .sources import SourceRepository
from .transaction import AtomicWriter, read_json


def snapshot_workspace_baseline(layout: ProjectLayout, session_id: str) -> dict[str, Any]:
    """Persist the completed current Workspace as the next incremental baseline."""
    paths = layout.session(session_id)
    manifest_path = paths.workspace / "data" / "manifest.json"
    entities_path = paths.workspace / "data" / "entities.jsonl"
    relations_path = paths.workspace / "data" / "relations.jsonl"
    missing = [
        path.relative_to(paths.workspace).as_posix()
        for path in (manifest_path, entities_path, relations_path)
        if not path.is_file()
    ]
    if missing:
        raise StateConflictError("Completed Workspace baseline is incomplete", missing=missing)
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
        raise ContractError("Completed Workspace source manifest is invalid", path=str(manifest_path))
    entities = [json.loads(line) for line in entities_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    relations = [json.loads(line) for line in relations_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = {
        "format_version": 1,
        "processed_source_ids": [
            str(item.get("source_id"))
            for item in manifest["sources"]
            if isinstance(item, dict) and str(item.get("source_id") or "").strip()
        ],
        "entities": entities,
        "relations": relations,
    }
    AtomicWriter(paths).json(paths.stages / "baseline" / "instances.json", payload)
    return payload


def workspace_catalog(layout: ProjectLayout) -> list[dict[str, Any]]:
    sessions = layout.data_root / "sessions"
    if not sessions.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for root in sessions.iterdir():
        if not root.is_dir():
            continue
        state_path = root / "intermediate" / "builder.json"
        readme_path = root / "workspace" / "README.md"
        manifest_path = root / "workspace" / "data" / "manifest.json"
        if not state_path.is_file() or not readme_path.is_file() or not manifest_path.is_file():
            continue
        try:
            state = read_json(state_path)
            manifest = read_json(manifest_path)
            readme = readme_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict) or state.get("status") != "workspace_ready":
            continue
        records.append(
            {
                "workspace_id": root.name,
                "updated_at": str(state.get("updated_at") or ""),
                "question": str(state.get("question") or ""),
                "readme_path": readme_path.relative_to(layout.project).as_posix(),
                "readme": readme,
                "problem_steps": [
                    str(step)
                    for step in ((state.get("problem") or {}).get("steps") or [])
                    if str(step).strip()
                ],
                "records": dict(manifest.get("records") or {}) if isinstance(manifest, dict) else {},
            }
        )
    records.sort(key=lambda item: item["updated_at"], reverse=True)
    return records


def install_extension_baseline(layout: ProjectLayout, session_id: str, base_workspace_id: str) -> dict[str, Any]:
    if session_id == base_workspace_id:
        snapshot_workspace_baseline(layout, session_id)
        state = read_json(layout.session(session_id).state / "builder.json")
        return state if isinstance(state, dict) else {}
    source = layout.session(base_workspace_id)
    target = layout.session(session_id)
    base_state = read_json(source.state / "builder.json")
    if not isinstance(base_state, dict) or base_state.get("status") != "workspace_ready":
        raise StateConflictError("Extension baseline must be a completed Workspace", workspace_id=base_workspace_id)
    required = (
        "README.md",
        "ontology/types.py",
        "ontology/schema.json",
        "data/entities.jsonl",
        "data/relations.jsonl",
        "data/manifest.json",
    )
    missing = [name for name in required if not (source.workspace / name).is_file()]
    if missing:
        raise ContractError("Extension baseline Workspace is incomplete", missing=missing)
    if target.workspace.exists():
        shutil.rmtree(target.workspace)
    shutil.copytree(source.workspace, target.workspace)
    clone_stage_files(source, target)
    for path in source.sources.rglob("*"):
        if not path.is_file():
            continue
        destination = target.sources / path.relative_to(source.sources)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(path, destination)
    repository = SourceRepository(target)
    current_records = repository.list()
    base_records = SourceRepository(source).list()
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in [*base_records, *current_records]:
        source_id = str(record.get("source_id") or "").strip()
        if source_id and source_id not in seen:
            seen.add(source_id)
            merged.append(dict(record))
    AtomicWriter(target).json(
        repository.manifest_path,
        {"format_version": 1, "sources": merged},
    )
    manifest = read_json(source.workspace / "data" / "manifest.json")
    entities = [
        json.loads(line)
        for line in (source.workspace / "data" / "entities.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    relations = [
        json.loads(line)
        for line in (source.workspace / "data" / "relations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    baseline = target.stages / "baseline"
    baseline.mkdir(parents=True, exist_ok=True)
    AtomicWriter(target).json(
        baseline / "instances.json",
        {
            "format_version": 1,
            "processed_source_ids": [
                str(item.get("source_id"))
                for item in (manifest.get("sources") or [])
                if isinstance(item, dict) and str(item.get("source_id") or "").strip()
            ],
            "entities": entities,
            "relations": relations,
        },
    )
    return base_state
