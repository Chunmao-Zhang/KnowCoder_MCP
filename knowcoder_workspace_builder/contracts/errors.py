"""Typed failures shared by Builder transports and services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TRANSIENT_EXTERNAL_ERROR_TYPES = frozenset(
    {
        "APIConnectionError",
        "APIError",
        "APITimeoutError",
        "ConnectTimeout",
        "InternalServerError",
        "RateLimitError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "ServiceUnavailableError",
        "TimeoutError",
    }
)


@dataclass(frozen=True)
class ErrorDetail:
    code: str
    message: str
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "context": dict(self.context)}


class BuilderError(RuntimeError):
    """Base error carrying a stable public code and non-secret context."""

    code = "builder_error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(self.code, message, context)


class ContractError(BuilderError):
    code = "contract_error"


class StorageBoundaryError(BuilderError):
    code = "storage_boundary_error"


class StateConflictError(BuilderError):
    code = "state_conflict"


class MissingStateError(BuilderError):
    code = "missing_state"


class UnsafeProjectError(BuilderError):
    code = "unsafe_selected_project"


class ExternalServiceError(BuilderError):
    code = "external_service_error"


class AgentProtocolError(BuilderError):
    code = "agent_protocol_error"


class InvocationTimeoutError(BuilderError):
    code = "invocation_timeout"


class AttemptCancelledError(BuilderError):
    code = "attempt_cancelled"
