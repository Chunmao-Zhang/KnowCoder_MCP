"""Shared mechanics for writing one active stage candidate."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import BuilderError, StateConflictError
from knowcoder_workspace_builder.runtime.candidate_normalization import record_normalization
from knowcoder_workspace_builder.runtime.invocation_context import InvocationContext, active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.storage.stage_artifacts import write_artifact


class BaseStageWriter:
    """Template for context-bound, deterministic stage persistence."""

    stage: str
    tool_name: str
    error_type: str
    expected_errors = (BuilderError, OSError, SyntaxError, TypeError, ValueError)

    def context(self) -> InvocationContext:
        context = active_invocation_context()
        if context.stage != self.stage:
            raise StateConflictError(
                "Tool belongs to a different active stage",
                expected=self.stage,
                actual=context.stage,
            )
        return context

    @staticmethod
    def paths() -> SessionPaths:
        return active_session_paths()

    def virtual(self, path: Path) -> str:
        return virtual_path_for(self.paths().root, path)

    def persist(self, name: str, value: Any, *, suffix: str = ".json") -> Path:
        context = self.context()
        return write_artifact(self.paths(), context.attempt_id, name, value, suffix=suffix)

    def normalization_log(self, changes: list[dict[str, str]]) -> str:
        return self.virtual(record_normalization(self.stage, self.tool_name, changes))

    @staticmethod
    def response(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def failure_payload(self, exc: Exception) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error_type": self.error_type, "error": str(exc)}
        if isinstance(exc, BuilderError):
            payload["error_code"] = exc.detail.code
            payload["error_context"] = dict(exc.detail.context)
        return payload

    def execute(self, operation: Callable[[], dict[str, Any]]) -> str:
        try:
            return self.response({"ok": True, **operation()})
        except self.expected_errors as exc:
            return self.response(self.failure_payload(exc))
