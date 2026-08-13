from __future__ import annotations

import logging

from knowcoder_workspace_builder.mcp.schemas import public_error
from knowcoder_workspace_builder.runtime.retry_policy import RETRY_DELAYS_SECONDS, retry_delay


def test_external_retry_schedule_is_shared_and_explicit() -> None:
    assert RETRY_DELAYS_SECONDS == (1, 2, 4, 8, 16)
    assert [retry_delay(number) for number in range(1, 6)] == [1, 2, 4, 8, 16]


def test_unknown_mcp_error_returns_traceable_id_and_logs_traceback(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="knowcoder_workspace_builder.mcp.schemas"):
        response = public_error(RuntimeError("private diagnostic detail"))

    context = response["error"]["context"]
    error_id = context["error_id"]
    assert response["error"]["code"] == "system_error"
    assert context["error_type"] == "RuntimeError"
    assert error_id in response["message"]
    assert "private diagnostic detail" not in str(response)
    assert error_id in caplog.text
    assert "private diagnostic detail" in caplog.text
