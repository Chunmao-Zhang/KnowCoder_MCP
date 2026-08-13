"""Read current and candidate Workspace metadata before incremental work."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.storage.canonical import canonical_index


MAX_CONTEXT_CHARS = 96_000
MAX_FILE_CHARS = 24_000


def _read_text(path: Path, remaining: int) -> tuple[str, int]:
    if not path.is_file() or remaining <= 0:
        return "", remaining
    text = path.read_text(encoding="utf-8")
    limit = min(MAX_FILE_CHARS, remaining)
    rendered = text[:limit]
    return rendered, max(0, remaining - len(rendered))


@tool
def workspace_readme_browser() -> str:
    """Return Workspace READMEs, canonical stage paths, and accepted stage files.

    Call this before stage work. A new build receives the project Workspace catalog.
    An extension also receives the selected Workspace README and accepted stage files.
    """
    paths = active_session_paths()
    context = active_invocation_context()
    workspace_context = context.input.get("workspace_context")
    workspace_context = workspace_context if isinstance(workspace_context, dict) else {}
    catalog = workspace_context.get("workspace_catalog")
    if not isinstance(catalog, list):
        catalog = []
    remaining = MAX_CONTEXT_CHARS
    readme, remaining = _read_text(paths.workspace / "README.md", remaining)
    manifest_text, remaining = _read_text(paths.workspace / "data" / "manifest.json", remaining)
    stage_files: dict[str, dict[str, Any]] = {}
    index = canonical_index(paths)
    for stage, files in index.items():
        result_path = paths.project / files["result"]
        artifact_path = paths.project / files["artifact"]
        result, remaining = _read_text(result_path, remaining)
        artifact, remaining = _read_text(artifact_path, remaining)
        stage_files[stage] = {
            "paths": files,
            "result": result,
            "artifact": artifact,
        }
    return json.dumps(
        {
            "ok": True,
            "workspace_exists": bool(readme),
            "readme": readme,
            "manifest": manifest_text,
            "canonical_stage_files": stage_files,
            "workspace_catalog": catalog,
            "truncated": remaining == 0,
        },
        ensure_ascii=False,
    )
