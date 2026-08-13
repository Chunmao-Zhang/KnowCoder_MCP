"""Read the accepted Schema outline during extraction."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from knowcoder_workspace_builder.contracts.errors import BuilderError, StateConflictError
from knowcoder_workspace_builder.runtime.invocation_context import active_invocation_context


@tool
def get_schema_outline() -> str:
    """Return the accepted Schema outline supplied to the active extraction stage."""
    try:
        context = active_invocation_context()
        if context.stage not in {"extract", "structured_extract"}:
            raise StateConflictError("Schema outline is available only during extraction", stage=context.stage)
        return json.dumps({"ok": True, "schema_outline": context.input["schema_outline"]}, ensure_ascii=False)
    except (BuilderError, TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error_type": "missing_schema_outline", "error": str(exc)}, ensure_ascii=False)
