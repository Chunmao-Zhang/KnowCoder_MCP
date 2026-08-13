"""Shared timeout budgets for Builder attempts and MCP calls."""

from __future__ import annotations

import os


AGENT_ATTEMPT_TIMEOUT_ENV = "SCHEMA_AGENT_ATTEMPT_TIMEOUT_SECONDS"
TRANSIENT_RETRY_LIMIT_ENV = "SCHEMA_BUILDER_TRANSIENT_RETRY_LIMIT"
MCP_TOOL_TIMEOUT_ENV = "HARNESS_MCP_TOOL_TIMEOUT_SECONDS"

DEFAULT_AGENT_ATTEMPT_TIMEOUT_SECONDS = 1_800.0
DEFAULT_TRANSIENT_RETRY_LIMIT = 5
MAX_AGENT_STAGES_BETWEEN_GATES = 3


def _positive_seconds(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be a positive number of seconds") from exc
    if value <= 0:
        raise ValueError(f"{env_name} must be a positive number of seconds")
    return value


def agent_attempt_timeout_seconds() -> float:
    """Return the maximum runtime for one Builder specialist attempt."""
    return _positive_seconds(AGENT_ATTEMPT_TIMEOUT_ENV, DEFAULT_AGENT_ATTEMPT_TIMEOUT_SECONDS)


def transient_retry_limit() -> int:
    """Return the number of automatic transient retries allowed per stage."""
    raw = os.environ.get(TRANSIENT_RETRY_LIMIT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TRANSIENT_RETRY_LIMIT
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{TRANSIENT_RETRY_LIMIT_ENV} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{TRANSIENT_RETRY_LIMIT_ENV} must be a non-negative integer")
    return value


def required_mcp_tool_budget_seconds() -> float:
    """Cover every allowed attempt in the longest uninterrupted Builder phase."""
    attempts_per_stage = transient_retry_limit() + 1
    return agent_attempt_timeout_seconds() * attempts_per_stage * MAX_AGENT_STAGES_BETWEEN_GATES


def mcp_tool_timeout_seconds() -> float:
    """Return the MCP read timeout and reject budgets that break automatic retry."""
    required = required_mcp_tool_budget_seconds()
    configured = _positive_seconds(MCP_TOOL_TIMEOUT_ENV, required)
    if configured < required:
        raise ValueError(
            f"{MCP_TOOL_TIMEOUT_ENV} must be at least {required:g} seconds to cover "
            "all configured Builder stage attempts"
        )
    return configured


DEFAULT_MCP_TOOL_TIMEOUT_SECONDS = (
    DEFAULT_AGENT_ATTEMPT_TIMEOUT_SECONDS
    * (DEFAULT_TRANSIENT_RETRY_LIMIT + 1)
    * MAX_AGENT_STAGES_BETWEEN_GATES
)
