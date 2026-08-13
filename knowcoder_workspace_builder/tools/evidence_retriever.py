"""Retrieve relevant chunks from one accepted current-Session evidence manifest."""

from __future__ import annotations

import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.harness.tools.web_search import web_search as _harness_web_search
from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.storage.transaction import AtomicWriter
from knowcoder_workspace_builder.storage.tool_calls import SearchLedger

from .web_content import ranked_search_urls, search_tokens
from .web_fetch import fetch_and_store_pages, load_web_fetch_settings


def _tokens(value: str) -> list[str]:
    return search_tokens(value)


def _search_signature(query: str) -> str:
    tokens = sorted(set(_tokens(query)))
    if not tokens:
        raise ValueError("Search query must contain searchable terms")
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def _perform_search(query: str, num_results: int) -> str:
    return _harness_web_search.invoke({"query": query, "num_results": num_results})


def _persist_search_bundle(
    signature: str,
    query: str,
    step_index: int,
    purpose: str,
    response: dict[str, Any],
) -> str:
    paths = active_session_paths()
    source_id = f"search_{signature[:16]}"
    target = paths.sources / "web_search" / f"{source_id}.json"
    results = list(response.get("results") or [])
    AtomicWriter(paths).json(
        target,
        {
            "format_version": 1,
            "source_id": source_id,
            "query": query,
            "step_index": step_index,
            "purpose": purpose,
            "results": results,
        },
    )
    return virtual_path_for(paths.root, target)


def _public_response(response: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in response.items() if key != "persisted"}


@tool
def web_search(
    query: str,
    step_index: int,
    purpose: str,
    expected_new_information: str,
    num_results: int = 3,
) -> str:
    """Search one unresolved evidence need with an explicit purpose and expected new fact."""
    try:
        context = active_invocation_context()
        if context.stage != "evidence":
            raise ValueError("web_search is available only during evidence collection")
        steps = list(context.input.get("steps") or [])
        if not isinstance(step_index, int) or isinstance(step_index, bool) or not 1 <= step_index <= len(steps):
            raise ValueError(f"step_index must be an integer from 1 through {len(steps)}")
        if not str(purpose).strip() or not str(expected_new_information).strip():
            raise ValueError("web_search requires purpose and expected_new_information")
        if not isinstance(num_results, int) or isinstance(num_results, bool) or not 1 <= num_results <= 5:
            raise ValueError("num_results must be an integer from 1 through 5")
        signature = _search_signature(query)
        ledger = SearchLedger(active_session_paths(), context.attempt_id)
        cached = ledger.find(signature)
        if cached is not None and cached.get("status") == "completed":
            payload = dict(cached.get("response") or {})
            payload.update(
                cached=True,
                reused_search_signature=signature,
            )
            return json.dumps(_public_response(payload), ensure_ascii=False)
        raw = _perform_search(query, num_results)
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise ValueError("Search service returned a non-object response")
        results = response.get("results")
        usable_results = [
            item
            for item in (results if isinstance(results, list) else [])
            if isinstance(item, dict)
            and any(str(item.get(field) or "").strip() for field in ("title", "link", "snippet"))
        ]
        failed = bool(response.get("error")) or not usable_results
        if not response.get("error") and not usable_results:
            response["error"] = "Search returned no usable results. Revise the query and retry the same step."
            response["error_type"] = "external_search_error"
        if not failed:
            response["results"] = usable_results
            response["discovery_path"] = _persist_search_bundle(signature, query, step_index, purpose, response)
            settings = load_web_fetch_settings()
            ranked_urls = ranked_search_urls(query, usable_results)
            fetched = fetch_and_store_pages(
                ranked_urls,
                query=f"{steps[step_index - 1]} {expected_new_information} {query}",
                target_successes=settings.successful_pages_per_search,
                settings=settings,
            )
            response["fetched_sources"] = fetched.get("sources") or []
            response["fetch_failures"] = fetched.get("failures") or []
            binding = dict(fetched.get("coverage_binding") or {})
            binding["step_index"] = step_index
            response["coverage_binding"] = binding
            failed = not bool(fetched.get("ok"))
            if failed:
                response["error"] = "Search found URLs, but no page produced usable complete content."
                response["error_type"] = "web_fetch_error"
        ledger.append(
            {
                "signature": signature,
                "step_index": step_index,
                "query": query,
                "purpose": purpose,
                "expected_new_information": expected_new_information,
                "status": "failed" if failed else "completed",
                "response": response,
            }
        )
        if failed:
            return json.dumps({"ok": False, "error_type": "external_search_error", **response}, ensure_ascii=False)
        successful_for_step = sum(
            1
            for item in ledger.records()
            if item.get("step_index") == step_index and item.get("status") == "completed"
        )
        if successful_for_step == 1:
            response["note"] = (
                "The first pass for this step is complete. Bind this source bundle to the step. "
                "Move to the next uncovered step. After all first passes, select one focused supplement when needed."
            )
        else:
            response["note"] = (
                "The focused supplement for this step is complete. Close this step. "
                "Record remaining limits in unresolved_gaps, then process another step or return the stage result."
            )
        return json.dumps(
            {
                "ok": True,
                **_public_response(response),
                "cached": False,
                "step_index": step_index,
            },
            ensure_ascii=False,
        )
    except ValueError as exc:
        return json.dumps({"ok": False, "error_type": "invalid_search_request", "error": str(exc)}, ensure_ascii=False)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        return json.dumps({"ok": False, "error_type": "external_search_error", "error": str(exc)}, ensure_ascii=False)


@tool
def web_search_batch(searches: list[dict[str, Any]]) -> str:
    """Run multiple evidence searches in one tool call and bind each result to its declared step."""
    if not isinstance(searches, list) or not searches:
        return json.dumps(
            {"ok": False, "error_type": "invalid_search_request", "error": "searches must be a non-empty list"},
            ensure_ascii=False,
        )
    prepared: list[dict[str, Any]] = []
    for position, search in enumerate(searches, start=1):
        if not isinstance(search, dict):
            return json.dumps(
                {
                    "ok": False,
                    "error_type": "invalid_search_request",
                    "error": f"searches item {position} must be an object",
                },
                ensure_ascii=False,
            )
        prepared.append(dict(search))

    settings = load_web_fetch_settings()
    worker_count = min(settings.max_concurrency, len(prepared))
    ordered: list[dict[str, Any] | None] = [None] * len(prepared)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(web_search.invoke, search): position
            for position, search in enumerate(prepared)
        }
        for future in as_completed(futures):
            position = futures[future]
            try:
                payload = json.loads(future.result())
                if not isinstance(payload, dict):
                    raise ValueError("Search result must be an object")
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                payload = {
                    "ok": False,
                    "error_type": "external_search_error",
                    "error": str(exc),
                }
            ordered[position] = {"position": position + 1, **payload}

    results = [item for item in ordered if item is not None]
    failures = [item for item in results if item.get("ok") is False]
    completed = len(results) - len(failures)
    status = "completed" if not failures else "partial" if completed else "failed"
    return json.dumps(
        {
            "ok": completed > 0,
            "status": status,
            "results": results,
            "completed": completed,
            "failed": len(failures),
            "failed_searches": [
                {
                    "position": item.get("position"),
                    "step_index": item.get("step_index"),
                    "error_type": item.get("error_type"),
                    "error": item.get("error"),
                }
                for item in failures
            ],
            "next_action": (
                "Keep the successful evidence. Retry only the failed searches with revised sources or queries."
                if failures and completed
                else "Continue evidence collection."
                if completed
                else "Revise the failed searches before continuing."
            ),
        },
        ensure_ascii=False,
    )
