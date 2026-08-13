"""web_search 工具

通用网页搜索，调用 Serper API。

搜索结果会自动落盘到当前 Session 的 `intermediate/sources/web_search/` 目录，并维护一个
按查询索引的缓存：后续轮次遇到相同查询时直接复用
已持久化的证据，而不会重复调用搜索接口。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.tools import tool

from knowcoder_workspace_builder.runtime.workspace_sources import (
    ensure_source_dirs,
    register_source_record,
    source_category_dir,
)
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_path_for
from knowcoder_workspace_builder.runtime.retry_policy import call_with_retries, is_external_api_error

_SEARCH_LOCK = threading.Lock()


@tool
def web_search(query: str, num_results: int = 10) -> str:
    """Search the web and return top results with title, link, and snippet.

    Results are persisted under the current Session's intermediate/sources/web_search/ so that
    later stages can reuse them without searching again. A repeated query returns the
    previously persisted evidence instead of issuing a new search.

    Args:
        query: Search query string.
        num_results: Number of results to return (default 10).
    """
    with _SEARCH_LOCK:
        cached = _cached_results(query)
        if cached is not None:
            persisted = _cached_source_refs(query)
            return json.dumps(
                {
                    "query": query,
                    "results": cached,
                    "cached": True,
                    "persisted": persisted,
                    "note": "Reused persisted web evidence from an earlier search in this run; no new search was issued.",
                },
                ensure_ascii=False,
            )

        service_cfg = _load_serper_config()

        file_key = _configured_secret(service_cfg.get("api_key", ""))
        # Prefer live project .env so key rotations take effect without full process restart.
        project_key = _configured_secret(_read_project_env_key("SERPER_API_KEY"))
        api_key = project_key or _configured_secret(os.environ.get("SERPER_API_KEY", "")) or file_key
        if not api_key:
            return json.dumps({"error": "SERPER_API_KEY not set"}, ensure_ascii=False)

        max_results = int(service_cfg.get("max_results_per_call", 5) or 5)
        default_results = int(service_cfg.get("default_num_results", 5) or 5)
        if not num_results:
            num_results = default_results
        num_results = max(1, min(int(num_results), max_results))

        payload = {"q": query, "num": num_results}
        headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

        try:
            def request_search() -> httpx.Response:
                response = httpx.post(
                    "https://google.serper.dev/search",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                return response

            resp = call_with_retries(request_search, is_retryable=is_external_api_error)
        except httpx.HTTPError as e:
            return json.dumps(
                {"error": f"Search failed: {e}", "error_type": "external_search_error"},
                ensure_ascii=False,
            )

        data = resp.json()
        organic = data.get("organic", [])[:num_results]
        results = [
            {
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
            for item in organic
        ]

        persisted = _persist_results(query, results)
    response: dict[str, object] = {"query": query, "results": results}
    if persisted:
        response["persisted"] = [
            {"source_id": rec["source_id"], "url": rec["url"], "title": rec["title"]}
            for rec in persisted
        ]
        response["note"] = (
            "Results persisted to intermediate/sources/web_search/ and registered for reuse. "
            "Register them in the evidence manifest sources using these source_ids."
        )
    return json.dumps(response, ensure_ascii=False)


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").strip().lower().split())



def _read_project_env_key(name: str) -> str:
    """Read one key from the project .env without overriding process env."""
    candidates = [
        Path(os.environ.get("SCHEMA_WORKSPACE_PROJECT", "") or ""),
        Path(os.environ.get("KNOWCODER_BUILDER_ROOT", "") or ""),
        Path(__file__).resolve().parents[2],
        Path(__file__).resolve().parents[3],
        Path.cwd(),
    ]
    for root in candidates:
        if not root:
            continue
        env_path = root / ".env"
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if not line.startswith(f"{name}="):
                    continue
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
        except OSError:
            continue
    return ""


def _configured_secret(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}"):
        return ""
    return text


def _web_evidence_dir() -> Path | None:
    run_env = os.environ.get("HARNESS_RUN_DIR", "")
    if not run_env:
        return None
    run = Path(run_env)
    ensure_source_dirs(run)
    return source_category_dir(run, "web_search")


def _read_cache(web_dir: Path) -> dict:
    cache_path = web_dir / "_cache.json"
    if not cache_path.exists():
        return {"queries": {}, "next_id": 1}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {"queries": {}, "next_id": 1}
    cache.setdefault("queries", {})
    cache.setdefault("next_id", 1)
    return cache


def _cached_results(query: str) -> list[dict] | None:
    web_dir = _web_evidence_dir()
    if web_dir is None or not web_dir.exists():
        return None
    cache = _read_cache(web_dir)
    entry = cache.get("queries", {}).get(_normalize_query(query))
    if not entry:
        return None
    if not entry.get("source_ids"):
        return []
    results = []
    for source_id in entry.get("source_ids", []):
        path = web_dir / f"{source_id}.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        results.append(
            {
                "title": record.get("title", ""),
                "link": record.get("url", ""),
                "snippet": record.get("snippet", ""),
            }
        )
    return results or None


def _cached_source_refs(query: str) -> list[dict]:
    web_dir = _web_evidence_dir()
    if web_dir is None or not web_dir.exists():
        return []
    entry = _read_cache(web_dir).get("queries", {}).get(_normalize_query(query), {})
    refs: list[dict] = []
    for source_id in entry.get("source_ids", []) if isinstance(entry, dict) else []:
        path = web_dir / f"{source_id}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        refs.append(
            {
                "source_id": str(record.get("source_id") or source_id),
                "url": str(record.get("url") or ""),
                "title": str(record.get("title") or ""),
            }
        )
    return refs


def _persist_results(query: str, results: list[dict]) -> list[dict]:
    web_dir = _web_evidence_dir()
    if web_dir is None:
        return []
    try:
        web_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    cache = _read_cache(web_dir)
    next_id = int(cache.get("next_id", 1) or 1)
    agent_id = os.environ.get("HARNESS_AGENT_ID", "")
    retrieved_at = datetime.now().isoformat(timespec="seconds")
    run_env = os.environ.get("HARNESS_RUN_DIR", "")
    run = Path(run_env).resolve() if run_env else None
    saved: list[dict] = []
    for item in results:
        source_id = f"web_{next_id:03d}"
        next_id += 1
        record = {
            "source_id": source_id,
            "query": query,
            "url": item.get("link", ""),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "retrieved_at": retrieved_at,
            "collected_by_agent": agent_id,
            "source_kind": "web",
            "evidence_group": "web_search",
        }
        try:
            path = web_dir / f"{source_id}.json"
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if run is not None:
                register_source_record(
                    run,
                    "web_search",
                    {
                        "ok": True,
                        "source_id": source_id,
                        "category": "web_search",
                        "source_kind": "web",
                        "evidence_group": "web_search",
                        "file_path": virtual_path_for(run, path),
                        "file_type": "json",
                        "url": record["url"],
                        "title": record["title"],
                        "reason": query,
                        "size_bytes": path.stat().st_size,
                        "retrieved_at": retrieved_at,
                    },
                )
        except Exception:
            continue
        saved.append(record)
    cache["queries"][_normalize_query(query)] = {"source_ids": [rec["source_id"] for rec in saved]}
    cache["next_id"] = next_id
    try:
        (web_dir / "_cache.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return saved


def _load_serper_config() -> dict:
    roots: list[Path] = []
    for value in (
        os.environ.get("HARNESS_ROOT", ""),
        os.environ.get("KNOWCODER_BUILDER_ROOT", ""),
        str(Path(__file__).resolve().parents[2]),
        os.getcwd(),
    ):
        if value:
            roots.append(Path(value).expanduser())
    config_path = next((root / "harness.json" for root in roots if (root / "harness.json").exists()), None)
    if config_path is None:
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    services = raw.get("services", {})
    serper = services.get("serper", {})
    return serper if isinstance(serper, dict) else {}
