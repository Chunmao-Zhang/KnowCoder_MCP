"""Persistence tool exposed only to the Problem Analyst."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.storage.stage_writers import ProblemWriter


@tool
def save_problem_review(
    workspace_action: str,
    scope: dict[str, Any],
    steps: list[str],
    missing_information: list[str],
    base_workspace_id: str | None = None,
) -> str:
    """Write the active Problem Analyst candidate to its fixed attempt file."""
    return ProblemWriter().save(
        workspace_action=workspace_action,
        base_workspace_id=base_workspace_id,
        scope=scope,
        steps=steps,
        missing_information=missing_information,
    )
