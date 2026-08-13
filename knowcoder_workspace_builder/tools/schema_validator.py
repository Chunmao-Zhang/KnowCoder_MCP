"""Validate the declared KO/OI single-file schema format."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from knowcoder_workspace_builder.contracts.errors import BuilderError
from knowcoder_workspace_builder.storage.schema import parse_schema

from .path_utils import resolve_path


@tool
def schema_validator(schema_source: str = "", schema_path: str = "") -> str:
    """Validate schema source or one current-Session schema file and return its outline."""
    try:
        if str(schema_source).strip():
            source = schema_source
        elif str(schema_path).strip():
            source = resolve_path(schema_path).read_text(encoding="utf-8")
        else:
            raise ValueError("schema_source or schema_path is required")
    except (OSError, UnicodeError, ValueError) as exc:
        return json.dumps({"ok": False, "valid": False, "findings": [], "errors": [str(exc)]}, ensure_ascii=False)
    try:
        schema = parse_schema(source, require_relations=False)
    except (BuilderError, ValueError) as exc:
        return json.dumps({"ok": True, "valid": False, "findings": [str(exc)], "errors": []}, ensure_ascii=False)
    return json.dumps(
        {"ok": True, "valid": True, "schema_outline": schema.outline(), "findings": [], "errors": []},
        ensure_ascii=False,
    )
