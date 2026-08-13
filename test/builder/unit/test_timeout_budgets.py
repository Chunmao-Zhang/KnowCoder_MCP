from __future__ import annotations

import pytest

from knowcoder_workspace_builder.runtime.timeouts import (
    AGENT_ATTEMPT_TIMEOUT_ENV,
    MCP_TOOL_TIMEOUT_ENV,
    TRANSIENT_RETRY_LIMIT_ENV,
    agent_attempt_timeout_seconds,
    mcp_tool_timeout_seconds,
    transient_retry_limit,
)


_TIMEOUT_ENV_NAMES = (
    AGENT_ATTEMPT_TIMEOUT_ENV,
    TRANSIENT_RETRY_LIMIT_ENV,
    MCP_TOOL_TIMEOUT_ENV,
)


def _clear_timeout_environment(monkeypatch) -> None:
    for name in _TIMEOUT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_default_outer_budgets_cover_all_attempts_between_review_gates(monkeypatch) -> None:
    _clear_timeout_environment(monkeypatch)

    assert agent_attempt_timeout_seconds() == 1_800
    assert transient_retry_limit() == 5
    assert mcp_tool_timeout_seconds() == 32_400


def test_outer_budgets_follow_attempt_and_retry_configuration(monkeypatch) -> None:
    _clear_timeout_environment(monkeypatch)
    monkeypatch.setenv(AGENT_ATTEMPT_TIMEOUT_ENV, "900")
    monkeypatch.setenv(TRANSIENT_RETRY_LIMIT_ENV, "2")

    assert mcp_tool_timeout_seconds() == 8_100


def test_mcp_override_cannot_disable_configured_automatic_retries(monkeypatch) -> None:
    _clear_timeout_environment(monkeypatch)
    monkeypatch.setenv(MCP_TOOL_TIMEOUT_ENV, "32399")

    with pytest.raises(ValueError, match="must be at least 32400 seconds"):
        mcp_tool_timeout_seconds()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (AGENT_ATTEMPT_TIMEOUT_ENV, "0", "positive number"),
        (TRANSIENT_RETRY_LIMIT_ENV, "-1", "non-negative integer"),
        (MCP_TOOL_TIMEOUT_ENV, "invalid", "positive number"),
    ],
)
def test_invalid_timeout_configuration_fails_fast(monkeypatch, name: str, value: str, message: str) -> None:
    _clear_timeout_environment(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        mcp_tool_timeout_seconds()
