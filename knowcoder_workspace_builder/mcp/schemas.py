"""MCP boundary normalization and public error payloads."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from knowcoder_workspace_builder.contracts.errors import BuilderError, ExternalServiceError, InvocationTimeoutError


LOGGER = logging.getLogger(__name__)


def normalize_upload_paths(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError("upload_paths must be a path string or a list of non-empty path strings")
    return [item.strip() for item in values]


def public_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, BuilderError):
        detail = exc.detail.to_dict()
        retryable = isinstance(exc, (ExternalServiceError, InvocationTimeoutError))
        return {
            "ok": False,
            "status": "failed",
            "error": detail,
            "message": detail["message"],
            "next_action": "retry" if retryable else "fix_input",
            "retryable": retryable,
        }
    error_id = uuid4().hex
    LOGGER.error(
        "Unexpected Builder MCP error [%s]",
        error_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return {
        "ok": False,
        "status": "failed",
        "error": {
            "code": "system_error",
            "message": "Builder encountered an unexpected system error",
            "context": {"error_id": error_id, "error_type": type(exc).__name__},
        },
        "message": f"Builder encountered an unexpected system error (error ID: {error_id})",
        "next_action": "none",
        "retryable": False,
    }
