"""Shared bounded parallel execution for Schema and Instance model units."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, TypeVar

from knowcoder_workspace_builder.contracts.errors import ExternalServiceError

Unit = TypeVar("Unit")
Result = TypeVar("Result")

CONSECUTIVE_FAILURE_LIMIT = 5


def run_parallel_units(
    units: Sequence[Unit],
    *,
    workers: int,
    operation: Callable[[Unit], Result],
    describe_error: Callable[[int, Unit, Exception], dict[str, Any]],
    on_progress: Callable[[int, int, str, int], None] | None,
    stage_name: str,
) -> tuple[dict[int, Result], dict[int, dict[str, Any]]]:
    """Skip failed units and stop the stage after five consecutive failures."""
    if workers < 1:
        raise ValueError("workers must be positive")
    if not units:
        raise ValueError(f"{stage_name} has no units to process")

    results: dict[int, Result] = {}
    skipped: dict[int, dict[str, Any]] = {}
    consecutive_failures = 0
    next_index = 0

    with ThreadPoolExecutor(max_workers=min(workers, len(units))) as pool:
        futures: dict[Future[Result], tuple[int, Unit]] = {}

        def submit(index: int) -> None:
            unit = units[index]
            if on_progress is not None:
                on_progress(index + 1, len(units), "running", len(results) + len(skipped))
            futures[pool.submit(operation, unit)] = (index, unit)

        while next_index < min(workers, len(units)):
            submit(next_index)
            next_index += 1

        while futures:
            future = next(as_completed(tuple(futures)))
            index, unit = futures.pop(future)
            try:
                results[index] = future.result()
            except Exception as exc:
                skipped[index] = describe_error(index, unit, exc)
                consecutive_failures += 1
                if on_progress is not None:
                    on_progress(index + 1, len(units), "skipped", len(results) + len(skipped))
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    for pending in futures:
                        pending.cancel()
                    raise ExternalServiceError(
                        f"{stage_name} stopped after {CONSECUTIVE_FAILURE_LIMIT} consecutive unit failures",
                        consecutive_failures=consecutive_failures,
                        last_error_type=type(exc).__name__,
                        last_error=str(exc),
                    ) from exc
            else:
                consecutive_failures = 0
                if on_progress is not None:
                    on_progress(index + 1, len(units), "done", len(results) + len(skipped))

            if next_index < len(units):
                submit(next_index)
                next_index += 1

    if not results:
        raise ExternalServiceError(f"{stage_name} produced no successful unit results")
    return results, skipped
