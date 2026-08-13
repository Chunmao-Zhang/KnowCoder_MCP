"""execute_code 工具

执行指定路径的 Python 脚本，支持传入命令行参数，返回执行结果。
Agent 先按当前 AGENT.md 的路径规则写入代码，再用本工具执行。
也可以直接执行 skills 目录下已有的脚本，通过 args 传入参数。

路径约定：与 deepagents 的 FilesystemBackend(virtual_mode=True) 一致，
输入的 file_path 是虚拟绝对路径（如 /workspaces/main/code/fib.py），
实际映射到 harness_root 下的对应相对路径。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from knowcoder_workspace_builder.harness.write_boundary import (
    WriteBoundaryError,
    execution_write_root,
    require_workspace_write_path,
    runtime_temporary_root,
)
from knowcoder_workspace_builder.runtime.session_context import SESSION_ROOT_ENV

MAX_OUTPUT_LENGTH = 10000

# harness_root 在运行时通过环境变量注入
_HARNESS_ROOT_ENV = "HARNESS_ROOT"
_TARGET_ROOT_ENV = "KNOWCODER_TARGET_PROJECT_ROOT"


def _harness_root() -> Path:
    return Path(os.environ.get(_HARNESS_ROOT_ENV, os.getcwd())).resolve()


def _target_root(root: Path | None = None) -> Path:
    value = os.environ.get(_TARGET_ROOT_ENV)
    if value:
        return Path(value).resolve()
    return (root or _harness_root()).resolve()


def _resolve_inside(base: Path, relative: str, *, virtual_path: str) -> Path:
    root = base.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise WriteBoundaryError(f"virtual path escapes its allowed root: {virtual_path}") from exc
    return candidate


def _resolve_virtual_path(virtual_path: str) -> Path:
    """将虚拟绝对路径解析为真实文件系统路径"""
    root = _harness_root()
    run_root = _target_root(root)
    value = str(virtual_path)
    session_root = os.environ.get(SESSION_ROOT_ENV, "").strip()
    session_is_target = bool(session_root) and Path(session_root).resolve() == run_root
    if value == "/.knowcoder_workspace":
        return (run_root if session_is_target else run_root / ".knowcoder_workspace").resolve()
    if value.startswith("/.knowcoder_workspace/"):
        relative = value.removeprefix("/.knowcoder_workspace/").lstrip("/")
        base = run_root if session_is_target else run_root / ".knowcoder_workspace"
        return _resolve_inside(base, relative, virtual_path=value)
    if value.startswith("/workspaces/"):
        relative = value.removeprefix("/workspaces/").lstrip("/")
        return _resolve_inside(run_root / ".knowcoder_workspace" / "workspaces", relative, virtual_path=value)
    if value.startswith("/large_tool_results/"):
        relative = value.removeprefix("/large_tool_results/").lstrip("/")
        return _resolve_inside(run_root / "large_tool_results", relative, virtual_path=value)
    if value.startswith("/"):
        return _resolve_inside(run_root, value.lstrip("/"), virtual_path=value)
    return _resolve_inside(run_root, value, virtual_path=value)


def _virtualize_text(text: str, root: Path) -> str:
    """Replace local harness paths in tool output with virtual paths."""
    if not text:
        return text
    run_root = _target_root(root)
    replacements = [
        (str(run_root / ".knowcoder_workspace" / "workspaces"), "/.knowcoder_workspace/workspaces"),
        (str(run_root / "runs" / "harness_conversation_logs"), "/runs/harness_conversation_logs"),
        (str(run_root / "large_tool_results"), "/large_tool_results"),
    ]
    value = text
    for source, target in replacements:
        value = value.replace(source, target)
    session_root = os.environ.get(SESSION_ROOT_ENV, "").strip()
    project_alias = (
        "/.knowcoder_workspace"
        if session_root and Path(session_root).resolve() == run_root
        else "/workspaces/project"
    )
    value = value.replace(str(run_root), project_alias)
    value = value.replace(str(root), "/workspaces/builder")
    return value


def _workspace_root_for_script(real_path: Path, run_root: Path) -> Path | None:
    session_root = os.environ.get(SESSION_ROOT_ENV, "").strip()
    if not session_root or Path(session_root).resolve() != run_root.resolve():
        return None
    try:
        real_path.relative_to(run_root / "intermediate" / "sources")
    except ValueError:
        return None
    return run_root


def _schema_import_contract_error(real_path: Path, workspace_root: Path | None) -> str:
    if workspace_root is None:
        return ""
    try:
        rel = real_path.relative_to(workspace_root)
    except ValueError:
        return ""
    if len(rel.parts) < 2 or rel.parts[0] != "sources":
        return ""
    workspace_dir = workspace_root / "workspace"
    required = [
        workspace_dir / "ontology" / "loader.py",
        workspace_dir / "ontology" / "schema.json",
        workspace_dir / "data" / "entities.jsonl",
        workspace_dir / "data" / "relations.jsonl",
        workspace_dir / "data" / "manifest.json",
    ]
    if not all(path.exists() for path in required):
        return ""
    try:
        content = real_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    current_markers = ("entities.jsonl", "relations.jsonl", "manifest.json", "load_workspace")
    if not any(marker in content for marker in current_markers):
        return ""
    if "load_workspace" in content or "ontology/loader.py" in content:
        return ""
    return (
        "Workspace analysis scripts must load workspace/ontology/loader.py before "
        "computing from workspace/data files. Update the script to call load_workspace, "
        "then call execute_code again."
    )


@tool
def execute_code(file_path: str, script_args: str = "", timeout: int = 120) -> str:
    """Execute a Python script at the given path and return stdout/stderr.

    Use write_file to save your code first, then call this tool to run it.
    The file_path should be an absolute virtual path (e.g. /workspaces/main/code/script.py).

    Args:
        file_path: Absolute path to the Python script (e.g. /workspaces/main/code/my_script.py).
        script_args: Command-line arguments to pass to the script (e.g. "--input /path/a.json --output /path/b.json").
        timeout: Maximum execution time in seconds (default 120).
    """
    if not file_path.startswith("/"):
        return json.dumps(
            {"status": "error", "error": "file_path must start with /"},
            ensure_ascii=False,
        )

    root = _harness_root()
    run_root = _target_root(root)
    try:
        real_path = require_workspace_write_path(file_path)
    except WriteBoundaryError as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    if not real_path.exists():
        return json.dumps(
            {
                "status": "error",
                "error": f"File not found: {file_path}. Write the script with write_file before calling execute_code.",
            },
            ensure_ascii=False,
        )

    if real_path.suffix != ".py":
        return json.dumps(
            {"status": "error", "error": "Only .py files are supported"},
            ensure_ascii=False,
        )

    workspace_root = _workspace_root_for_script(real_path, run_root)
    contract_error = _schema_import_contract_error(real_path, workspace_root)
    if contract_error:
        return json.dumps({"status": "error", "error": contract_error}, ensure_ascii=False)

    cmd = [
        sys.executable,
        "-m",
        "knowcoder_workspace_builder.harness.tools.restricted_runner",
        str(real_path),
    ]
    if script_args:
        # Resolve virtual paths in args to real paths
        try:
            resolved_args = _resolve_args(script_args)
        except WriteBoundaryError as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        cmd.extend(shlex.split(resolved_args))

    env = os.environ.copy()
    env["HARNESS_SCRIPT_PATH"] = str(real_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    runtime_tmp = runtime_temporary_root()
    runtime_tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(runtime_tmp)
    env["TMP"] = str(runtime_tmp)
    env["TEMP"] = str(runtime_tmp)
    package_root = str(Path(__file__).resolve().parents[3])
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (package_root, current_pythonpath) if part
    )
    # Run with cwd at the execution write root (the session's intermediate dir) so that a
    # model script using a relative output/batch path writes it where the persistence tool
    # (append_instances_batches_from_file) actually reads it, instead of the script's dir.
    try:
        execution_root = execution_write_root()
        script_cwd = str(execution_root)
        real_path.relative_to(execution_root)
        env["HARNESS_RUN_DIR"] = str(real_path.parent)
    except WriteBoundaryError:
        script_cwd = str(real_path.parent)
        if workspace_root is not None:
            env["HARNESS_RUN_DIR"] = str(workspace_root)
    except ValueError:
        if workspace_root is not None:
            env["HARNESS_RUN_DIR"] = str(workspace_root)
    if workspace_root is not None:
        env["ONTOLOGY_RUN_DIR"] = str(workspace_root)

    try:
        result = subprocess.run(
            cmd,
            cwd=script_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"status": "error", "error": f"Execution timed out ({timeout}s)"},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"status": "error", "error": f"Execution failed: {e}"},
            ensure_ascii=False,
        )

    stdout = _virtualize_text(result.stdout, root)
    if len(stdout) > MAX_OUTPUT_LENGTH:
        stdout = stdout[:MAX_OUTPUT_LENGTH] + "\n... [truncated]"

    if result.returncode != 0:
        stderr = _virtualize_text(result.stderr, root)
        stderr = stderr[:3000] if stderr else ""
        return json.dumps(
            {
                "status": "error",
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
            ensure_ascii=False,
        )

    return json.dumps({"status": "success", "output": stdout}, ensure_ascii=False)


def _resolve_args(args: str) -> str:
    """Resolve virtual paths in args string to real filesystem paths.

    Absolute virtual path tokens are converted to real target-project paths.
    """
    tokens = shlex.split(args)
    resolved = []
    for token in tokens:
        if token.startswith("/"):
            resolved.append(str(_resolve_virtual_path(token)))
        elif "=" in token and token.split("=", 1)[1].startswith("/"):
            name, value = token.split("=", 1)
            resolved.append(f"{name}={_resolve_virtual_path(value)}")
        else:
            resolved.append(token)
    # Re-quote tokens that contain spaces
    return " ".join(shlex.quote(t) for t in resolved)
