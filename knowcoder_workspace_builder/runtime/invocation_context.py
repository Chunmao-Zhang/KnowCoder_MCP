"""Persist and load the immutable input for one Agent attempt."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError, MissingStateError
from knowcoder_workspace_builder.storage.paths import SessionPaths
from knowcoder_workspace_builder.storage.transaction import AtomicWriter, read_json
from knowcoder_workspace_builder.validation.inputs import validate_stage_input

from .session_context import ATTEMPT_ID_ENV, active_session_paths


@dataclass(frozen=True)
class InvocationContext:
    session_id: str
    attempt_id: str
    stage: str
    input: dict[str, Any]


_ACTIVE_DELEGATION: ContextVar[dict[str, Any] | None] = ContextVar(
    "active_builder_delegation",
    default=None,
)


@contextmanager
def bind_delegation_payload(payload: dict[str, Any]) -> Iterator[None]:
    """Bind the complete trusted payload used by the Coordinator's next task call."""
    if not isinstance(payload, dict):
        raise ContractError("Coordinator delegation payload must be an object")
    stage = str(payload.get("stage") or "").strip()
    stage_input = payload.get("input")
    normalized = validate_stage_input(stage, stage_input)
    trusted_payload = {**payload, "stage": stage, "input": normalized}
    token = _ACTIVE_DELEGATION.set(trusted_payload)
    try:
        yield
    finally:
        _ACTIVE_DELEGATION.reset(token)


def active_delegation_payload() -> dict[str, Any]:
    value = _ACTIVE_DELEGATION.get()
    if value is None:
        raise MissingStateError("Active Coordinator delegation payload is missing")
    stage = str(value.get("stage") or "").strip()
    stage_input = validate_stage_input(stage, value.get("input"))
    return {**value, "stage": stage, "input": stage_input}


def write_invocation_context(
    paths: SessionPaths,
    attempt_id: str,
    stage: str,
    stage_input: dict[str, Any],
) -> InvocationContext:
    normalized = validate_stage_input(stage, stage_input)
    context = InvocationContext(paths.session_id, attempt_id, stage, normalized)
    target = paths.attempts / attempt_id / "context.json"
    AtomicWriter(paths).json(
        target,
        {
            "session_id": context.session_id,
            "attempt_id": context.attempt_id,
            "stage": context.stage,
            "input": context.input,
        },
    )
    return context


def active_invocation_context() -> InvocationContext:
    paths = active_session_paths()
    attempt_id = os.environ.get(ATTEMPT_ID_ENV, "").strip()
    if not attempt_id:
        raise MissingStateError("Active invocation attempt ID is missing")
    target = paths.attempts / attempt_id / "context.json"
    if not target.is_file():
        raise MissingStateError("Active invocation context does not exist", attempt_id=attempt_id)
    value = read_json(target)
    if not isinstance(value, dict) or not isinstance(value.get("input"), dict):
        raise ContractError("Active invocation context is invalid", attempt_id=attempt_id)
    if value.get("session_id") != paths.session_id or value.get("attempt_id") != attempt_id:
        raise ContractError("Active invocation context has conflicting ownership", attempt_id=attempt_id)
    stage = str(value.get("stage") or "")
    stage_input = validate_stage_input(stage, value["input"])
    return InvocationContext(paths.session_id, attempt_id, stage, stage_input)
