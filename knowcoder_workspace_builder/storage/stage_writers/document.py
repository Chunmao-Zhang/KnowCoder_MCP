"""Workspace Documenter candidate writer."""

from __future__ import annotations

from typing import Any

from knowcoder_workspace_builder.storage.readme import render_workspace_readme
from knowcoder_workspace_builder.storage.schema import parse_schema

from .base import BaseStageWriter


class DocumentWriter(BaseStageWriter):
    stage = "document"
    tool_name = "save_workspace_readme"
    error_type = "workspace_readme_write_failed"

    def save(
        self,
        *,
        name: str,
        description: str,
        summary: str,
        incremental_guidance: str,
    ) -> str:
        def operation() -> dict[str, Any]:
            stage_input = self.context().input
            instance_summary = dict(stage_input["instance_summary"])
            workspace_context = dict(stage_input.get("workspace_context") or {})
            content = render_workspace_readme(
                {
                    "name": name,
                    "description": description,
                    "summary": summary,
                    "incremental_guidance": incremental_guidance,
                },
                problem=dict(stage_input["problem"]),
                schema=parse_schema(str(stage_input["schema_source"]), require_relations=False),
                entity_count=int(instance_summary["entity_count"]),
                relation_count=int(instance_summary["relation_count"]),
                sources=list(stage_input["sources"]),
                workspace_mode=str(workspace_context.get("workspace_mode") or "new"),
                base_workspace_id=str(workspace_context.get("base_workspace_id") or ""),
            )
            target = self.persist("workspace_readme", content, suffix=".md")
            return {"candidate_path": self.virtual(target)}

        return self.execute(operation)
