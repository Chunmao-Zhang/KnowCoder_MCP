"""Framework-neutral MCP setup and current Workspace handoff contracts."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.contracts.workspace import PUBLIC_WORKSPACE_FILES
from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.storage.transaction import read_json

SERVER_INSTRUCTIONS = """# KnowCoder Workspace Builder

## Purpose

Build and extend a durable, source-grounded Workspace for any task that needs deep research. Prefer KnowCoder when the requested result needs external knowledge, several sources, broad or deep coverage, traceable evidence, structured comparison, or later reuse. The user does not need to mention MCP or Workspace. Keep routing a model judgement for ambiguous requests.

## Why Use It

KnowCoder preserves the research plan, Schema, complete sources, extracted entities and relations, provenance, and a readable Workspace summary. It supports user review, interruption recovery, and incremental research without repeating accepted work.

## Tools

- `start_workspace_task`: start new research, extend an existing Workspace, or recover a failed task.
- `wait_for_task_update`: wait once for background progress.
- `submit_review_decision`: submit a Problem or Schema confirmation or natural-language revision.
- `read_workspace`: read accepted Workspace resources in bounded pages.
- `find_workspace_tasks`: find an exact task or Workspace when its identifier is unavailable.
- `stop_task`: stop a running background task.

## Workflow

1. Call `start_workspace_task` with the complete user request. Include `workspace_id` only for an incremental update.
2. Keep the returned `continuation_token` unchanged.
3. While status is `running`, call one `wait_for_task_update` with `timeout_seconds` set to 40 or less. Start the next wait only after the previous call returns.
4. Keep ordinary waits silent. A no-change timeout and continued work in the same stage require no user-facing message.
5. When a different stage or Subagent starts and a wait returns `event=stage_started`, pause before the next tool call. Give the user one short sentence naming the newly started stage or Subagent. Then continue waiting. Treat this progress sentence as part of the same turn; it does not end the task.
6. Report a Review, completion, or error when returned.
7. When `next_action` is `present_review`, briefly introduce the result before showing the saved read-only HTML link. For a Problem Review, summarize the confirmed scope and main research steps. For a Schema Review, summarize the main entity types, important attributes, and relations. Use two to four concise sentences, then tell the user they can confirm or request changes in this conversation. The page is a durable snapshot inside the Workspace and needs no Review Server. End the current turn.
8. A Review is a human decision boundary. End the assistant turn that presents it. Call `submit_review_decision` only in a later turn whose current user message explicitly confirms the displayed Review or requests a revision. The original research request is not approval. A waiting status, Review version, link, tool error, or your own judgement is not approval. Never confirm on the user's behalf. Pass the displayed `continuation_token` and `expected_version`. Use `action="confirm"` for explicit acceptance. Use `action="revise"` with the user's complete change request in `instruction`.
9. Continue silent serial waiting after the decision starts background work.
10. When completed, call `read_workspace`, beginning with `readme`, then read only the resources needed for the answer.
11. For a later research extension, call `start_workspace_task` with the new request and the existing `workspace_id`.

At a Review, tell the user that confirmation and revision happen in this Agent conversation. Do not wait or infer approval. Do not read completed Workspace resources before completion. During polling, do not narrate elapsed time, repeated status, continued work, or the next wait. If a KnowCoder call fails, report the failure and stop this research turn. Keep the existing task available for inspection or recovery; do not confirm, revise, or replace the Workspace workflow as error recovery. When a token is unavailable, call `find_workspace_tasks` and let the user choose among multiple matches.

## Results

Running results report the current stage and next action. Review results contain the user-readable content, version, and a permanent local HTML snapshot. Completion returns the Workspace ID and entry point. Errors identify the failed operation and stage and state whether recovery is available. Treat identifiers, versions, and tokens as opaque values.

## Scope

Use KnowCoder for any deep-research task whose answer benefits from persistent, verified, reusable evidence. Use simpler host tools for a narrow fact that needs no durable research Workspace. Once a KnowCoder task starts, follow its lifecycle until the next Review or terminal result.

## Examples

- Research recent implementation routes, representative projects, evaluations, costs, and boundaries for an emerging technology.
- Compare a market across products, companies, pricing, adoption, evidence quality, and unresolved risks.
- Build a sourced profile and timeline from official records, publications, and credible reporting.
"""


WORKSPACE_STRUCTURE = """workspace/
  README.md
  workspace.yaml
  ontology/
    README.md
    types.py
    loader.py
    schema.json
  knowledge/
  data/
    entities.jsonl
    relations.jsonl
    source_chunks.jsonl
    manifest.json
    source/
      <content-addressed>.md
intermediate/
  sources/
  stages/
    problem/
    evidence/
    schema_build/
    schema_judge/
    extract/
    structured_extract/
    document/
    baseline/
  attempts/
  tool_results/
  runtime_tmp/
  builder.json
  events.jsonl
  conversation.jsonl
  source_manifest.json
  current_answer.md
"""


def server_instructions(session_id: str = "") -> str:
    """Return portable instructions with optional host-owned Session context."""
    current = str(session_id or "").strip()
    if not current:
        return SERVER_INSTRUCTIONS
    return (
        f"{SERVER_INSTRUCTIONS}\n"
        f"The host bound this MCP process to workspace_id `{current}`. Use this workspace_id for current-Session tools."
    )


def workspace_handoff(paths: SessionPaths) -> dict[str, Any]:
    files = {
        name: paths.relative_to_project(paths.workspace / name)
        for name in PUBLIC_WORKSPACE_FILES
    }
    problem_path = paths.stages / "problem" / "problem.json"
    if not problem_path.is_file():
        raise ValueError("Completed Workspace is missing its confirmed Problem artifact")
    problem = read_json(problem_path)
    raw_steps = problem.get("steps") if isinstance(problem, dict) else None
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Completed Workspace has no confirmed research steps")
    confirmed_steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw_steps, start=1):
        if isinstance(item, dict):
            text = str(item.get("description") or item.get("title") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            confirmed_steps.append({"index": index, "text": text})
    if not confirmed_steps:
        raise ValueError("Completed Workspace confirmed research steps are empty")
    return {
        "session_id": paths.session_id,
        "workspace_root": paths.relative_to_project(paths.workspace),
        "workspace_structure": WORKSPACE_STRUCTURE,
        "required_files": files,
        "intermediate_root": paths.relative_to_project(paths.intermediate),
        "confirmed_research_steps": confirmed_steps,
        "instruction": (
            "Read individual Workspace artifacts as needed through read_workspace. "
            "Read complete source text only when needed by passing its manifest workspace_path. "
            "Use offset pagination for large files and keep the accepted Workspace read-only. "
            "Write optional analysis intermediates only below intermediate_root. "
            "Derive calculations and the final answer from the files actually read. "
            "Map instance source_refs to source records and cite their recorded public links. "
            "Keep every absolute, relative Workspace, and virtual path out of the user-visible answer."
        ),
        "write_rules": [
            "Write analysis intermediates only under intermediate_root.",
            "Do not write outside this Session under .knowcoder_workspace.",
            "Keep the accepted Workspace read-only during solving.",
        ],
    }
