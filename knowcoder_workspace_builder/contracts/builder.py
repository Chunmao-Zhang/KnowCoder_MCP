"""Builder command, status, confirmation, and handoff contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ContractError


BUILD_STATUSES = frozenset(
    {
        "running",
        "needs_problem_confirmation",
        "needs_schema_confirmation",
        "workspace_ready",
        "failed",
        "cancelled",
    }
)
NEXT_ACTIONS = frozenset(
    {
        "wait",
        "confirm_problem",
        "confirm_schema",
        "resume_builder",
        "read_workspace",
        "retry",
        "none",
    }
)


@dataclass(frozen=True)
class BuildResponse:
    ok: bool
    session_id: str
    status: str
    stage: str
    version: int
    next_action: str
    message: str
    review: dict[str, Any] | None = None
    workspace: dict[str, str] | None = None
    errors: tuple[dict[str, Any], ...] = ()
    events_after: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ContractError("Builder response requires a Session ID")
        if self.status not in BUILD_STATUSES:
            raise ContractError("Builder response has an invalid status", status=self.status)
        if self.next_action not in NEXT_ACTIONS:
            raise ContractError("Builder response has an invalid next action", next_action=self.next_action)
        if self.version < 1:
            raise ContractError("Builder response version must be positive", version=self.version)
        if not self.message.strip():
            raise ContractError("Builder response requires a message")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "workspace_id": self.session_id,
            "status": self.status,
            "stage": self.stage,
            "version": self.version,
            "next_action": self.next_action,
            "message": self.message,
            "review": self.review,
            "workspace": self.workspace,
            "errors": [dict(item) for item in self.errors],
            "events_after": self.events_after,
            "metadata": dict(self.metadata),
        }
