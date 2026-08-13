from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[3]


def test_package_installs_with_harness_prompts_and_stdio_entrypoint(runtime_project: Path) -> None:
    case_root = runtime_project / ".knowcoder_workspace" / "test_runs" / "package_install"
    source_copy = case_root / "source"
    install_root = case_root / "installed"
    source_copy.mkdir(parents=True)
    shutil.copy2(SOURCE_ROOT / "pyproject.toml", source_copy / "pyproject.toml")
    shutil.copytree(
        SOURCE_ROOT / "knowcoder_workspace_builder",
        source_copy / "knowcoder_workspace_builder",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(install_root), str(source_copy)],
        check=True,
        cwd=source_copy,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    script = """
import asyncio
import json
from pathlib import Path
import knowcoder_workspace_builder as package
from knowcoder_workspace_builder.mcp.server import create_server
root = Path(package.__file__).resolve().parent
print(json.dumps({
    "tools": sorted(tool.name for tool in asyncio.run(create_server().list_tools())),
    "harness": (root / "harness.json").is_file() and (root / "harness" / "run.py").is_file(),
    "prompts": (root / "subagents" / "schema_builder" / "AGENT.md").is_file(),
    "skill": (root / "skills" / "report-writer" / "SKILL.md").is_file(),
}))
"""
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(install_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=runtime_project,
        capture_output=True,
        text=True,
        env=environment,
    )
    payload = json.loads(result.stdout)

    assert payload["harness"] is True
    assert payload["prompts"] is True
    assert payload["skill"] is True
    assert payload["tools"] == [
        "find_workspace_tasks",
        "read_workspace",
        "start_workspace_task",
        "stop_task",
        "submit_review_decision",
        "wait_for_task_update",
    ]
