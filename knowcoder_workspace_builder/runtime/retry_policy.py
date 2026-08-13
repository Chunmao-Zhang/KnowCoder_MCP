"""One retry policy for transient external service failures."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from knowcoder_workspace_builder.contracts.errors import TRANSIENT_EXTERNAL_ERROR_TYPES


T = TypeVar("T")
RETRY_DELAYS_SECONDS = (1, 2, 4, 8, 16)


def is_external_api_error(exc: BaseException) -> bool:
    """Recognize transport and provider failures without coupling to one SDK."""
    error_type = type(exc).__name__
    if error_type in TRANSIENT_EXTERNAL_ERROR_TYPES:
        return True
    module = type(exc).__module__.split(".", 1)[0]
    return module in {"httpx", "httpcore", "openai"}


def retry_delay(retry_number: int) -> int:
    if not isinstance(retry_number, int) or isinstance(retry_number, bool):
        raise ValueError("retry_number must be an integer")
    if not 1 <= retry_number <= len(RETRY_DELAYS_SECONDS):
        raise ValueError(f"retry_number must be from 1 through {len(RETRY_DELAYS_SECONDS)}")
    return RETRY_DELAYS_SECONDS[retry_number - 1]


def wait_before_retry(retry_number: int) -> None:
    time.sleep(retry_delay(retry_number))


def call_with_retries(
    operation: Callable[[], T],
    *,
    is_retryable: Callable[[BaseException], bool],
    max_retries: int | None = None,
) -> T:
    retry_limit = len(RETRY_DELAYS_SECONDS) if max_retries is None else max_retries
    if not isinstance(retry_limit, int) or isinstance(retry_limit, bool) or not 0 <= retry_limit <= len(RETRY_DELAYS_SECONDS):
        raise ValueError(f"max_retries must be from 0 through {len(RETRY_DELAYS_SECONDS)}")
    for retry_number in range(0, retry_limit + 1):
        try:
            return operation()
        except BaseException as exc:
            if retry_number >= retry_limit or not is_retryable(exc):
                raise
            wait_before_retry(retry_number + 1)
    raise RuntimeError("Retry loop ended without a result")
