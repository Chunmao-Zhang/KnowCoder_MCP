"""Agent invocation and completion report contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ContractError


TERMINAL_RESULT_STATUSES = frozenset({"completed", "failed", "skipped", "cancelled"})


@dataclass(frozen=True)
class StageResult:
    """One immutable Subagent result with separate public and private fields."""

    ok: bool
    stage: str
    status: str
    report: str
    handoff: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    metrics: dict[str, int | float | str | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        stage = self.stage.strip()
        report = self.report.strip()
        if not stage:
            raise ContractError("Stage result requires a stage")
        if self.status not in TERMINAL_RESULT_STATUSES:
            raise ContractError("Stage result has an invalid terminal status", status=self.status)
        if not report:
            raise ContractError("Stage result requires a non-empty completion report", stage=stage)
        if self.ok and self.status not in {"completed", "skipped"}:
            raise ContractError("A successful result must be completed or skipped", status=self.status)
        if not self.ok and self.status in {"completed", "skipped"}:
            raise ContractError("A failed result cannot use a successful status", status=self.status)
        if not isinstance(self.handoff, dict):
            raise ContractError("Stage result handoff must be an object", stage=stage)
        for key, value in self.artifacts.items():
            if not str(key).strip() or not str(value).strip():
                raise ContractError("Artifact keys and paths must be non-empty", stage=stage)

    def to_dict(self, *, include_private: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "stage": self.stage,
            "status": self.status,
            "report": self.report,
            "artifacts": dict(self.artifacts),
            "errors": list(self.errors),
            "limitations": list(self.limitations),
            "metrics": dict(self.metrics),
        }
        if include_private:
            payload["handoff"] = dict(self.handoff)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StageResult":
        if not isinstance(value, dict):
            raise ContractError("Stage result must be an object")
        return cls(
            ok=bool(value.get("ok")),
            stage=str(value.get("stage") or ""),
            status=str(value.get("status") or ""),
            report=str(value.get("report") or ""),
            handoff=dict(value.get("handoff") or {}),
            artifacts={str(key): str(path) for key, path in dict(value.get("artifacts") or {}).items()},
            errors=tuple(str(item) for item in value.get("errors") or []),
            limitations=tuple(str(item) for item in value.get("limitations") or []),
            metrics=dict(value.get("metrics") or {}),
        )
