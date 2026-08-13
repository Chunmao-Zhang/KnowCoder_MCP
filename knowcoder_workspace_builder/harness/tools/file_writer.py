"""Controlled file-writing tool for harness agents."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from knowcoder_workspace_builder.harness.write_boundary import (
    WriteBoundaryError,
    require_workspace_write_path,
    selected_project_root,
)


@tool
def write_file(file_path: str, content: str) -> str:
    """Write complete text content to an absolute virtual path.

    Args:
        file_path: Absolute virtual path beginning with /.
        content: Complete file content to write.
    """
    if not file_path.startswith("/"):
        return json.dumps(
            {"status": "error", "error": "file_path must start with /"},
            ensure_ascii=False,
        )

    try:
        real_path = require_workspace_write_path(file_path)
        root = selected_project_root()
    except WriteBoundaryError as exc:
        return json.dumps(
            {"status": "error", "error": str(exc)},
            ensure_ascii=False,
        )

    if real_path.exists() and real_path.is_dir():
        return json.dumps(
            {"status": "error", "error": "file_path points to a directory"},
            ensure_ascii=False,
        )

    try:
        real_path.parent.mkdir(parents=True, exist_ok=True)
        real_path.write_text(content, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - returned to the model for repair.
        return json.dumps(
            {"status": "error", "error": f"write failed: {type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        )

    rel_path = real_path.relative_to(root).as_posix()
    return json.dumps(
        {
            "status": "success",
            "path": "/" + rel_path,
            "bytes": len(content.encode("utf-8")),
        },
        ensure_ascii=False,
    )
