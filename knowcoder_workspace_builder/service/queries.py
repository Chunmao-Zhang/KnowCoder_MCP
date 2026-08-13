"""Read-only Builder status, event, review, and Workspace queries."""

from __future__ import annotations

import json
from typing import Any

from knowcoder_workspace_builder.contracts.builder import BuildResponse
from knowcoder_workspace_builder.contracts.integration import workspace_handoff
from knowcoder_workspace_builder.contracts.workspace import PUBLIC_WORKSPACE_FILES
from knowcoder_workspace_builder.storage.events import EventStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.workspace import WorkspaceRepository
from knowcoder_workspace_builder.workflow.models import BuildState


_NEXT_ACTION = {
    "running": "resume_builder",
    "needs_problem_confirmation": "confirm_problem",
    "needs_schema_confirmation": "confirm_schema",
    "workspace_ready": "read_workspace",
    "failed": "retry",
    "cancelled": "retry",
}

_MESSAGE = {
    "running": "Builder is ready to run the next declared stage.",
    "needs_problem_confirmation": "The clarified problem and research steps require user confirmation.",
    "needs_schema_confirmation": "The judged schema requires user confirmation.",
    "workspace_ready": "The executable knowledge Workspace is ready for the Solver.",
    "failed": "The current Subagent could not produce a validated result. Review the failure details, then retry the stage.",
    "cancelled": "Builder was cancelled.",
}


class BuildQueries:
    def __init__(self, layout: ProjectLayout) -> None:
        self.layout = layout
        self.states = BuildStateStore(layout)
        self.events = EventStore(layout)

    def response(self, state: BuildState) -> BuildResponse:
        paths = self.layout.session(state.session_id)
        events = self.events.read(state.session_id, public_only=True)
        latest_sequence = max((int(item.get("sequence") or 0) for item in events), default=0)
        review: dict[str, Any] | None = None
        if state.status == "needs_problem_confirmation":
            review = {
                **dict(state.problem or {}),
                "workspace_id": state.session_id,
                "review_type": "problem",
                "version": state.version,
            }
        elif state.status == "needs_schema_confirmation":
            review = {
                **dict(state.schema_review or {}),
                "workspace_id": state.session_id,
                "review_type": "schema",
                "version": state.version,
            }
        workspace = None
        next_tool = {
            "failed": "retry_workspace_build",
            "cancelled": "retry_workspace_build",
            "workspace_ready": "read_workspace_artifact",
        }.get(state.status, "resume_workspace_build")
        metadata: dict[str, Any] = {
            "next_tool": next_tool,
            "confirmation_returns_to_host": state.status.startswith("needs_"),
            "invalidated_from": state.invalidated_from or None,
        }
        if state.status == "workspace_ready":
            workspace = {
                name: paths.relative_to_project(paths.workspace / name)
                for name in PUBLIC_WORKSPACE_FILES
            }
            metadata["workspace_handoff"] = workspace_handoff(paths)
        errors: tuple[dict[str, Any], ...] = ()
        if state.failure:
            errors = (dict(state.failure),)
        return BuildResponse(
            ok=state.status not in {"failed", "cancelled"},
            session_id=state.session_id,
            status=state.status,
            stage=str(state.stage),
            version=state.version,
            next_action=_NEXT_ACTION[state.status],
            message=_MESSAGE[state.status],
            review=review,
            workspace=workspace,
            errors=errors,
            events_after=latest_sequence,
            metadata=metadata,
        )

    def status(self, session_id: str) -> BuildResponse:
        return self.response(self.states.load(session_id))

    def public_events(self, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        return self.events.read(session_id, after=after, public_only=True)

    def read_artifact(self, session_id: str, artifact: str, *, offset: int = 0, max_chars: int = 20_000) -> dict[str, Any]:
        paths = self.layout.session(session_id)
        if artifact == "workspace_bundle":
            repository = WorkspaceRepository(paths)
            repository.validate_ready()
            name = "workspace_bundle"
            path = paths.workspace
            text = json.dumps(
                {
                    filename: repository.read_artifact(filename).read_text(encoding="utf-8")
                    for filename in PUBLIC_WORKSPACE_FILES
                },
                ensure_ascii=False,
            )
        else:
            aliases = {
                "schema": "ontology/types.py",
                "types": "ontology/types.py",
                "schema_contract": "ontology/schema.json",
                "loader": "ontology/loader.py",
                "instances": "data/entities.jsonl",
                "entities": "data/entities.jsonl",
                "relations": "data/relations.jsonl",
                "workspace_readme": "README.md",
                "data_audit": "data/manifest.json",
                "manifest": "data/manifest.json",
                "instances_jsonl": "data/entities.jsonl",
            }
            name = aliases.get(artifact, artifact)
            path = WorkspaceRepository(paths).read_artifact(name)
            text = path.read_text(encoding="utf-8")
        start = max(0, int(offset))
        limit = min(max(1, int(max_chars)), 100_000)
        content = text[start : start + limit]
        next_offset = start + len(content)
        return {
            "ok": True,
            "status": "artifact_read",
            "session_id": session_id,
            "workspace_id": session_id,
            "artifact": artifact,
            "resolved_artifact": name,
            "path": paths.relative_to_project(path),
            "content": content,
            "offset": start,
            "total_chars": len(text),
            "next_offset": next_offset if next_offset < len(text) else None,
        }

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "session_id": state.session_id,
                "workspace_id": state.session_id,
                "question": state.question,
                "status": state.status,
                "stage": str(state.stage),
                "version": state.version,
                "updated_at": state.updated_at,
            }
            for state in self.states.list_states(limit)
        ]
