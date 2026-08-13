"""File-backed stage candidates, validation logs, and repair feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json
from knowcoder_workspace_builder.validation.repair_prompts import resolve_repair_prompt


# One initial pass plus at most one targeted file repair. Business and external
# service failures are not repair loops.
MAX_VALIDATION_ROUNDS = 2

STAGE_CANDIDATE_FILES: dict[str, tuple[str, str]] = {
    "problem": ("problem_review", ".json"),
    "evidence": ("evidence_manifest", ".json"),
    "schema_build": ("schema_draft", ".py"),
    "schema_judge": ("schema_judgement", ".json"),
    "extract": ("unstructured_draft", ".json"),
    "structured_extract": ("structured_draft", ".json"),
    "document": ("workspace_readme", ".md"),
}

STAGE_PERSISTENCE_TOOLS: dict[str, str] = {
    "problem": "save_problem_review",
    "evidence": "save_evidence_manifest",
    "schema_build": "save_schema",
    "schema_judge": "save_schema_judgement",
    "extract": "extract_unstructured_chunks",
    "structured_extract": "append_instances_batches_from_file",
    "document": "save_workspace_readme",
}


def validation_log_path() -> Path:
    context = active_invocation_context()
    paths = active_session_paths()
    return paths.attempts / context.attempt_id / "validation_log.json"


def load_repair_prompt(
    stage: str,
    *,
    errors: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Return a case-based repair prompt for the current validation errors."""
    return resolve_repair_prompt(
        stage,
        mode="completion",
        errors=errors or [],
        context=context or {},
    )


def record_validation_round(
    stage: str,
    *,
    round_index: int,
    ok: bool,
    errors: list[str],
    candidate_path: str,
    repair_prompt: str = "",
    feedback: dict[str, Any] | None = None,
) -> None:
    context = active_invocation_context()
    paths = active_session_paths()
    path = validation_log_path()
    current: dict[str, Any]
    if path.is_file():
        loaded = read_json(path)
        current = loaded if isinstance(loaded, dict) else {"format_version": 1, "rounds": []}
    else:
        current = {"format_version": 1, "stage": stage, "attempt_id": context.attempt_id, "rounds": []}
    rounds = list(current.get("rounds") or [])
    rounds.append(
        {
            "round": round_index,
            "ok": ok,
            "errors": list(errors),
            "candidate_path": candidate_path,
            "repair_prompt": repair_prompt,
            "feedback": feedback or {},
        }
    )
    current.update(
        {
            "format_version": 1,
            "stage": stage,
            "attempt_id": context.attempt_id,
            "rounds": rounds,
            "max_rounds": MAX_VALIDATION_ROUNDS,
        }
    )
    AtomicWriter(paths).json(path, current)
