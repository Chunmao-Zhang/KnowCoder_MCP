"""Render and validate the model-authored Workspace README contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .schema import ParsedSchema


REQUIRED_BODY_HEADINGS = (
    "# Workspace Overview",
    "## Completed Research",
    "## Schema and Data",
    "## Main Files",
    "## Sources",
    "## Incremental Extension",
)

def source_workspace_path(source_id: str) -> str:
    filename = f"{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:16]}.md"
    return f"data/source/{filename}"


def _yaml_text(value: object) -> str:
    return json.dumps(str(value or "").strip(), ensure_ascii=False)


def render_workspace_readme(
    model_content: dict[str, Any],
    *,
    problem: dict[str, Any],
    schema: ParsedSchema,
    entity_count: int,
    relation_count: int,
    sources: list[dict[str, Any]],
    workspace_mode: str,
    base_workspace_id: str,
) -> str:
    name = str(model_content.get("name") or "").strip()
    description = str(model_content.get("description") or "").strip()
    summary = str(model_content.get("summary") or "").strip()
    incremental_guidance = str(model_content.get("incremental_guidance") or "").strip()
    missing = [
        field
        for field, value in (("name", name), ("description", description))
        if not value
    ]
    if missing:
        raise ContractError("Workspace README model content is incomplete", missing=missing)
    entity_names = ", ".join(entity.name for entity in schema.entities)
    relation_names = ", ".join(sorted(schema.relation_names))
    steps = [str(step).strip() for step in problem.get("steps") or [] if str(step).strip()]
    finish_details = (
        f"Completed {len(steps)} confirmed research steps for {name}. "
        f"Published {len(schema.entities)} entity types, {len(schema.relation_names)} relation types, "
        f"{entity_count} entities, {relation_count} relations, and {len(sources)} sources."
    )
    lines = [
        "---",
        f"name: {_yaml_text(name)}",
        f"description: {_yaml_text(description)}",
        "finish:",
        "  completed: true",
        f"  details: {_yaml_text(finish_details)}",
        "---",
        "",
        "# Workspace Overview",
        "",
        summary,
        "",
        f"- Mode: `{workspace_mode}`",
        f"- Base Workspace: `{base_workspace_id}`" if base_workspace_id else "- Base Workspace: none",
        "",
        "## Completed Research",
        "",
    ]
    lines.extend(f"- {step}" for step in steps)
    lines.extend(
        [
            "",
            "## Schema and Data",
            "",
            f"- Entity types: {entity_names}",
            f"- Relation types: {relation_names}",
            f"- Entities: {entity_count}",
            f"- Relations: {relation_count}",
            f"- Sources: {len(sources)}",
            "",
            "## Main Files",
            "",
            "- `ontology/types.py`: executable Schema.",
            "- `ontology/schema.json`: language-neutral Schema contract.",
            "- `ontology/loader.py`: instance loader and graph validator.",
            "- `data/entities.jsonl`: validated entity instances.",
            "- `data/relations.jsonl`: validated relation instances.",
            "- `data/manifest.json`: versions, counts, and source provenance.",
            "- `data/source/`: normalized source documents.",
        ]
    )
    lines.extend(["", "## Sources", ""])
    for source in sources:
        source_id = str(source.get("source_id") or "").strip()
        title = str(source.get("title") or source_id).strip()
        if source_id:
            lines.append(f"- {title}: `{source_workspace_path(source_id)}`")
    lines.extend(["", "## Incremental Extension", "", incremental_guidance, ""])
    return "\n".join(lines)


def validate_workspace_readme(value: str) -> dict[str, Any]:
    text = str(value or "")
    lines = text.splitlines()
    if len(lines) < 8 or lines[0] != "---":
        raise ContractError("Workspace README requires YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ContractError("Workspace README frontmatter is not closed") from exc
    frontmatter = lines[1:end]
    if len(frontmatter) != 5:
        raise ContractError("Workspace README frontmatter requires name, description, and finish")
    if not frontmatter[0].startswith("name: ") or not frontmatter[1].startswith("description: "):
        raise ContractError("Workspace README frontmatter field order is invalid")
    if frontmatter[2] != "finish:" or frontmatter[3] != "  completed: true":
        raise ContractError("Workspace README finish.completed must be true")
    if not frontmatter[4].startswith("  details: "):
        raise ContractError("Workspace README finish.details is required")
    try:
        name = json.loads(frontmatter[0].split(": ", 1)[1])
        description = json.loads(frontmatter[1].split(": ", 1)[1])
        details = json.loads(frontmatter[4].split(": ", 1)[1])
    except json.JSONDecodeError as exc:
        raise ContractError("Workspace README frontmatter text must be quoted YAML strings") from exc
    if not all(isinstance(item, str) and item.strip() for item in (name, description, details)):
        raise ContractError("Workspace README frontmatter values must be non-empty")
    body = "\n".join(lines[end + 1 :])
    missing = [heading for heading in REQUIRED_BODY_HEADINGS if heading not in body]
    if missing:
        raise ContractError("Workspace README body is incomplete", missing=missing)
    path_errors: list[dict[str, object]] = []
    for line_number, line in enumerate(lines[end + 1 :], start=end + 2):
        reasons: list[str] = []
        if "\\" in line:
            reasons.append("uses Windows path separators")
        if "/.knowcoder_workspace/" in line or ".knowcoder_workspace/sessions/" in line:
            reasons.append("references the private Session or intermediate directory")
        if reasons:
            path_errors.append(
                {
                    "line": line_number,
                    "text": line.strip(),
                    "reason": "; ".join(reasons),
                }
            )
    if path_errors:
        raise ContractError(
            "Workspace README contains non-public paths. Use forward slashes and public Workspace-relative paths only, "
            "such as ontology/schema.json, data/entities.jsonl, data/relations.jsonl, data/manifest.json, "
            "and data/source/<file>.md",
            invalid_paths=path_errors,
        )
    return {
        "name": name,
        "description": description,
        "finish": {"completed": True, "details": details},
        "body": body,
    }
