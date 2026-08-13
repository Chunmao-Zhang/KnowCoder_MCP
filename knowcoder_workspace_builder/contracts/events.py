"""Immutable Builder event contracts for host projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .errors import ContractError


EVENT_VISIBILITY = frozenset({"public", "private"})
EVENT_STATUSES = frozenset({"running", "completed", "failed", "skipped", "cancelled", "waiting"})


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class InvocationEvent:
    session_id: str
    sequence: int
    kind: str
    status: str
    event_id: str = field(default_factory=lambda: str(uuid4()))
    turn_id: str = ""
    invocation_id: str = ""
    attempt_id: str = ""
    agent: str = ""
    stage: str = ""
    attempt_number: int = 1
    timestamp: str = field(default_factory=utc_now)
    report: str = ""
    public_data: dict[str, Any] = field(default_factory=dict)
    private_data: dict[str, Any] = field(default_factory=dict)
    visibility: str = "public"

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ContractError("Event requires a Session ID")
        if self.sequence < 1:
            raise ContractError("Event sequence must be positive", sequence=self.sequence)
        if not self.kind.strip():
            raise ContractError("Event requires a kind")
        if self.status not in EVENT_STATUSES:
            raise ContractError("Event has an invalid status", status=self.status)
        if self.visibility not in EVENT_VISIBILITY:
            raise ContractError("Event has invalid visibility", visibility=self.visibility)
        if self.attempt_number < 1:
            raise ContractError("Attempt number must be positive", attempt_number=self.attempt_number)

    def to_dict(self, *, include_private: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "status": self.status,
            "turn_id": self.turn_id,
            "invocation_id": self.invocation_id,
            "attempt_id": self.attempt_id,
            "agent": self.agent,
            "stage": self.stage,
            "attempt_number": self.attempt_number,
            "timestamp": self.timestamp,
            "report": self.report,
            "data": dict(self.public_data),
            "visibility": self.visibility,
        }
        if include_private:
            payload["private"] = dict(self.private_data)
        return payload

    def to_public_dict(self) -> dict[str, Any] | None:
        if self.visibility != "public":
            return None
        return self.to_dict(include_private=False)
