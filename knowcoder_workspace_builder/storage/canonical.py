"""Stable accepted-stage files used by later incremental work."""

from __future__ import annotations

import shutil
from pathlib import Path
from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import ContractError

from .paths import SessionPaths, is_within
from .transaction import AtomicWriter


CANONICAL_ARTIFACTS: dict[str, tuple[str, str]] = {
    "problem": ("problem.json", "problem_review"),
    "evidence": ("evidence.json", "evidence_manifest"),
    "schema_build": ("schema.py", "schema_draft"),
    "schema_judge": ("judgement.json", "schema_judgement"),
    "extract": ("instances.json", "unstructured_draft"),
    "structured_extract": ("instances.json", "structured_draft"),
    "document": ("README.md", "workspace_readme"),
}


def stage_directory(paths: SessionPaths, stage: str) -> Path:
    if stage not in CANONICAL_ARTIFACTS:
        raise ContractError("Unknown canonical stage", stage=stage)
    return paths.stages / stage


def canonical_artifact_path(paths: SessionPaths, stage: str) -> Path:
    filename, _artifact = CANONICAL_ARTIFACTS[stage]
    return stage_directory(paths, stage) / filename


def canonical_index(paths: SessionPaths) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for stage in CANONICAL_ARTIFACTS:
        directory = stage_directory(paths, stage)
        artifact = canonical_artifact_path(paths, stage)
        result = directory / "result.json"
        validation = directory / "validation" / "validation.json"
        files = {
            "result": paths.relative_to_project(result),
            "artifact": paths.relative_to_project(artifact),
            "validation": paths.relative_to_project(validation),
        }
        if any(path.is_file() for path in (result, artifact, validation)):
            index[stage] = files
    return index


def _resolve_artifact(paths: SessionPaths, value: str) -> Path:
    prefix = "/.knowcoder_workspace/"
    candidate = paths.root / value.removeprefix(prefix) if value.startswith(prefix) else paths.project / value
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContractError("Accepted stage artifact does not exist", path=str(candidate)) from exc
    if not is_within(resolved, paths.root.resolve(strict=True)):
        raise ContractError("Accepted stage artifact is outside the Session", path=str(resolved))
    return resolved


def publish_stage_result(paths: SessionPaths, stage: str, attempt_id: str, result: StageResult) -> dict[str, str]:
    """Publish one validated attempt through stable stage-owned paths."""
    directory = stage_directory(paths, stage)
    validation_directory = directory / "validation"
    directory.mkdir(parents=True, exist_ok=True)
    validation_directory.mkdir(parents=True, exist_ok=True)
    writer = AtomicWriter(paths)
    writer.json(
        directory / "result.json",
        {
            "format_version": 1,
            "stage": stage,
            "attempt_id": attempt_id,
            "status": result.status,
            "handoff": dict(result.handoff),
            "artifacts": dict(result.artifacts),
        },
    )
    artifact_name = CANONICAL_ARTIFACTS[stage][1]
    artifact_value = str(result.artifacts.get(artifact_name) or "").strip()
    target = canonical_artifact_path(paths, stage)
    if artifact_value:
        source = _resolve_artifact(paths, artifact_value)
        writer.text(target, source.read_text(encoding="utf-8"))
    elif result.status == "skipped" and stage in {"extract", "structured_extract"}:
        writer.json(
            target,
            {
                "format_version": 1,
                "processed_source_ids": [],
                "entities": [],
                "relations": [],
            },
        )
    elif result.status != "skipped":
        raise ContractError("Validated stage result is missing its canonical artifact", stage=stage)
    validation_source = paths.attempts / attempt_id / "validation_log.json"
    if validation_source.is_file():
        writer.text(validation_directory / "validation.json", validation_source.read_text(encoding="utf-8"))
    else:
        writer.json(
            validation_directory / "validation.json",
            {
                "format_version": 1,
                "stage": stage,
                "attempt_id": attempt_id,
                "rounds": [{"round": 1, "ok": True, "errors": []}],
            },
        )
    return {
        "result": paths.relative_to_project(directory / "result.json"),
        "artifact": paths.relative_to_project(target),
        "validation": paths.relative_to_project(validation_directory / "validation.json"),
    }


def clone_stage_files(source: SessionPaths, target: SessionPaths) -> None:
    if source.stages.is_dir():
        shutil.copytree(source.stages, target.stages, dirs_exist_ok=True)
