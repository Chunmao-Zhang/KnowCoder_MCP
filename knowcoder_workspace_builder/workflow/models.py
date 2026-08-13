"""Session, build, attempt, invocation, source, and version values."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .stages import Stage


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BuildState:
    session_id: str
    question: str
    upload_paths: list[str]
    status: str = "running"
    stage: str = Stage.PROBLEM
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    problem: dict[str, Any] | None = None
    problem_confirmed: bool = False
    evidence: dict[str, Any] | None = None
    schema_review: dict[str, Any] | None = None
    schema_confirmed: bool = False
    extraction: dict[str, Any] = field(default_factory=dict)
    documentation: dict[str, Any] | None = None
    active_attempt_id: str = ""
    accepted_attempts: dict[str, str] = field(default_factory=dict)
    stage_attempts: dict[str, int] = field(default_factory=dict)
    schema_revision_rounds: int = 0
    invalidated_from: str = ""
    pending_revision: str = ""
    active_follow_up_request: str = ""
    retry_reason: str = ""
    pending_evidence_step_indexes: list[int] = field(default_factory=list)
    replace_instances: bool = False
    workspace_mode: str = "new"
    base_workspace_id: str = ""
    extension_baseline_steps: list[str] = field(default_factory=list)
    failure: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ContractError("Build state requires a Session ID")
        if not self.question.strip():
            raise ContractError("Build state requires a question")
        if self.version < 1:
            raise ContractError("Build state version must be positive", version=self.version)
        if self.schema_revision_rounds < 0:
            raise ContractError("Schema revision rounds cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BuildState":
        if not isinstance(value, dict):
            raise ContractError("Build state must be an object")
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})
