"""Installed command for serving and diagnosing KnowCoder MCP."""

from __future__ import annotations

import argparse
import asyncio
import os
import runpy
import sys
import tempfile
from pathlib import Path

import httpx
from openai import OpenAI

from knowcoder_workspace_builder import __version__
from knowcoder_workspace_builder.config import (
    apply_settings,
    default_config_path,
    load_settings,
)
from knowcoder_workspace_builder.mcp.server import create_server
from knowcoder_workspace_builder.runtime.retry_policy import (
    call_with_retries,
    is_external_api_error,
)
from knowcoder_workspace_builder.storage.project import default_project_root

EXPECTED_MCP_TOOLS = {
    "find_workspace_tasks",
    "read_workspace",
    "start_workspace_task",
    "stop_task",
    "submit_review_decision",
    "wait_for_task_update",
}


async def _check_crawl4ai_browser() -> None:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        result = await crawler.arun(
            url="raw:<html><body><main>KnowCoder browser check</main></body></html>",
            config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS),
        )
    if not result.success or "KnowCoder browser check" not in str(result.markdown):
        raise RuntimeError(f"Crawl4AI browser check failed: {result.error_message or 'rendered text is missing'}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowcoder-mcp")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="Run the stdio MCP Server")
    serve.add_argument(
        "project",
        nargs="?",
        help="Optional data root. Global registrations use the shared user-level KnowCoder directory.",
    )
    doctor_parser = subcommands.add_parser("doctor", help="Check the local installation or configured services")
    doctor_parser.add_argument(
        "--local",
        action="store_true",
        help="Check installation without calling model or search APIs",
    )
    return parser


def _check_model(label: str, *, api_key: str, base_url: str, model: str) -> None:
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=20)
    call_with_retries(
        lambda: client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=4,
            temperature=0,
        ),
        is_retryable=is_external_api_error,
    )
    print(f"PASS {label}: {model}")


def _check_local_installation() -> None:
    config_path = default_config_path()
    print(f"PASS command: knowcoder-mcp {__version__}")
    if config_path.is_file():
        runpy.run_path(str(config_path))
        print(f"PASS configuration file: {config_path}")
        try:
            load_settings(config_path)
        except Exception as exc:  # noqa: BLE001 - local check reports incomplete user configuration without hiding it.
            print(f"WARN configuration incomplete: {exc}")
    else:
        print(f"WARN configuration file missing: {config_path}")
    with tempfile.TemporaryDirectory(prefix="knowcoder-mcp-doctor-") as directory:
        path = Path(directory) / "write-check.txt"
        path.write_text("ok", encoding="utf-8")
    print("PASS local Workspace writes")
    tools = {tool.name for tool in asyncio.run(create_server().list_tools())}
    if tools != EXPECTED_MCP_TOOLS:
        raise RuntimeError(f"MCP Server exposed unexpected tools: {sorted(tools)}")
    print(f"PASS MCP initialization: {len(tools)} tools")
    asyncio.run(_check_crawl4ai_browser())
    print("PASS Crawl4AI and Chromium")


def doctor(*, local_only: bool = False) -> int:
    try:
        _check_local_installation()
        if local_only:
            print("PASS local installation; no model or search API was called")
            return 0
        settings = load_settings()
        print("PASS configuration")
        _check_model("research model", **settings.research.__dict__)
        _check_model("extraction model", **settings.extraction.__dict__)
        response = call_with_retries(
            lambda: httpx.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
                json={"q": "KnowCoder MCP connectivity check", "num": 1},
                timeout=20,
            ),
            is_retryable=is_external_api_error,
        )
        response.raise_for_status()
        print("PASS Serper")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports the exact failed check.
        print(f"FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    args = _parser().parse_args()
    command = args.command or "serve"
    if command == "doctor":
        raise SystemExit(doctor(local_only=args.local))
    settings = load_settings()
    apply_settings(settings)
    project_argument = getattr(args, "project", None)
    project = Path(project_argument).expanduser() if project_argument else default_project_root()
    if project_argument is None:
        project.mkdir(parents=True, exist_ok=True)
    project = project.resolve()
    if not project.is_dir():
        raise SystemExit(f"Selected project is not a directory: {project}")
    os.environ["SCHEMA_WORKSPACE_PROJECT"] = str(project)
    os.chdir(project)
    create_server().run(transport="stdio")
