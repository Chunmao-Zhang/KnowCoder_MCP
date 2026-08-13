"""Builder service facade shared by MCP and review transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.agents.runner import AgentRunner, HarnessAgentRunner
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.project import SelectedProject
from knowcoder_workspace_builder.runtime.timeouts import transient_retry_limit as configured_transient_retry_limit

from .commands import BuildCommands
from .coordinator import Coordinator
from .queries import BuildQueries
from .stage_runner import StageRunner


class BuilderService:
    def __init__(
        self,
        selected_project: SelectedProject | str | Path,
        *,
        agent_runner: AgentRunner | None = None,
        schema_revision_limit: int = 5,
        transient_retry_limit: int | None = None,
        recover_interrupted: bool = True,
    ) -> None:
        self.selected = (
            selected_project
            if isinstance(selected_project, SelectedProject)
            else SelectedProject.resolve(selected_project)
        )
        self.layout = ProjectLayout(self.selected)
        self.agent_runner = agent_runner or HarnessAgentRunner()
        self.stage_runner = StageRunner(self.layout, self.agent_runner)
        retry_limit = configured_transient_retry_limit() if transient_retry_limit is None else transient_retry_limit
        self.coordinator = Coordinator(
            self.layout,
            self.stage_runner,
            schema_revision_limit=schema_revision_limit,
            transient_retry_limit=retry_limit,
        )
        self.commands = BuildCommands(self.layout, self.coordinator, self.agent_runner)
        self.queries = BuildQueries(self.layout)
        if recover_interrupted:
            self.commands.recover_interrupted()

    @property
    def target_project_dir(self) -> str:
        return str(self.selected.root)

    def _require_target(self, value: str | Path | None) -> None:
        self.selected.require_match(value)

    def start_workspace_build(
        self,
        *,
        question: str,
        upload_paths: list[str] | None = None,
        target_project_dir: str | None = None,
        workspace_id: str | None = None,
        turn_id: str = "",
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        state = self.commands.start(
            question=question,
            upload_paths=list(upload_paths or []),
            session_id=workspace_id,
            turn_id=turn_id,
        )
        return self.queries.response(state).to_dict()

    def prepare_workspace_build(
        self,
        *,
        question: str,
        upload_paths: list[str] | None = None,
        target_project_dir: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a running Session for a detached MCP first phase."""
        self._require_target(target_project_dir)
        state = self.commands.prepare_start(
            question=question,
            upload_paths=list(upload_paths or []),
            session_id=workspace_id,
        )
        return self.queries.response(state).to_dict()

    def resume_workspace_build(
        self,
        *,
        workspace_id: str,
        confirmation_type: str = "",
        user_confirmed: bool = True,
        user_instruction: str = "",
        target_project_dir: str | None = None,
        expected_version: int | None = None,
        turn_id: str = "",
        follow_up_request: str = "",
        change_impacts: list[str] | None = None,
        evidence_step_indexes: list[int] | None = None,
        upload_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        state = self.commands.resume(
            workspace_id,
            confirmation_type=confirmation_type,
            user_confirmed=user_confirmed,
            user_instruction=user_instruction,
            expected_version=expected_version,
            turn_id=turn_id,
            follow_up_request=follow_up_request,
            change_impacts=change_impacts,
            evidence_step_indexes=evidence_step_indexes,
            upload_paths=upload_paths,
        )
        return self.queries.response(state).to_dict()

    def retry_workspace_build(
        self,
        *,
        workspace_id: str,
        reason: str,
        target_project_dir: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        state = self.commands.retry(workspace_id, reason=reason, expected_version=expected_version)
        return self.queries.response(state).to_dict()

    def fail_background_job(
        self,
        *,
        workspace_id: str,
        expected_version: int,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.commands.fail_background_job(
            workspace_id,
            expected_version=expected_version,
            failure=failure,
        )
        return self.queries.response(state).to_dict()

    def cancel_workspace_build(
        self,
        *,
        workspace_id: str,
        target_project_dir: str | None = None,
        expected_version: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        state = self.commands.cancel(workspace_id, expected_version=expected_version, reason=reason)
        return self.queries.response(state).to_dict()

    def delete_workspace_build(
        self,
        *,
        workspace_id: str,
        target_project_dir: str | None = None,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        return self.commands.delete(workspace_id)

    def get_workspace_status(
        self,
        *,
        workspace_id: str,
        target_project_dir: str | None = None,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        return self.queries.status(workspace_id).to_dict()

    def get_workspace_events(
        self,
        *,
        workspace_id: str,
        after: int = 0,
        target_project_dir: str | None = None,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        events = self.queries.public_events(workspace_id, after=after)
        return {"ok": True, "session_id": workspace_id, "workspace_id": workspace_id, "events": events}

    def read_workspace_artifact(
        self,
        *,
        workspace_id: str,
        artifact: str,
        target_project_dir: str | None = None,
        offset: int = 0,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        return self.queries.read_artifact(workspace_id, artifact, offset=offset, max_chars=max_chars)

    def list_workspaces(
        self,
        *,
        target_project_dir: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        return {"ok": True, "workspaces": self.queries.list_sessions(limit)}

    def list_workspace_readmes(
        self,
        *,
        target_project_dir: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._require_target(target_project_dir)
        records: list[dict[str, Any]] = []
        for item in self.queries.list_sessions(limit):
            if item["status"] != "workspace_ready":
                continue
            path = self.layout.session(item["session_id"]).workspace / "README.md"
            records.append({**item, "readme": path.read_text(encoding="utf-8")})
        return {"ok": True, "workspaces": records}
