"""Validate the declared KO/OI single-file schema format."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from knowcoder_workspace_builder.contracts.errors import BuilderError
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_invocation_context,
)
from knowcoder_workspace_builder.storage.schema import parse_schema

from .path_utils import resolve_path


@tool
def schema_validator(schema_source: str = "", schema_path: str = "") -> str:
    """Validate schema source or one current-Session schema file and return its outline."""
    try:
        try:
            context = active_invocation_context()
        except BuilderError:
            context = None
        if context is not None and context.stage == "schema_judge":
            source = str(context.input.get("schema_source") or "").strip()
            if not source:
                raise ValueError("The Schema review input does not contain schema_source")
            source_origin = "invocation_context"
        elif str(schema_source).strip():
            source = schema_source
            source_origin = "argument"
        elif str(schema_path).strip():
            source = resolve_path(schema_path).read_text(encoding="utf-8")
            source_origin = "file"
        else:
            raise ValueError("schema_source or schema_path is required")
    except (OSError, UnicodeError, ValueError) as exc:
        return json.dumps({"ok": False, "valid": False, "findings": [], "errors": [str(exc)]}, ensure_ascii=False)
    try:
        schema = parse_schema(source, require_relations=False)
    except (BuilderError, ValueError) as exc:
        return json.dumps({"ok": True, "valid": False, "findings": [str(exc)], "errors": []}, ensure_ascii=False)
    return json.dumps(
        {
            "ok": True,
            "valid": True,
            "source_origin": source_origin,
            "schema_outline": schema.outline(),
            "findings": [],
            "errors": [],
        },
        ensure_ascii=False,
    )
