"""Check that role Prompts declare the same fields as executable contracts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.contracts.errors import ContractError

from .inputs import INPUT_FIELDS
from .stage_results import STAGE_PROTOCOLS


PROMPT_FOR_STAGE = {
    "problem": "problem_clarifier",
    "evidence": "evidence_collector",
    "schema_build": "schema_builder",
    "schema_judge": "schema_judger",
    "extract": "data_extractor",
    "structured_extract": "structured_data_extractor",
    "document": "workspace_documenter",
}
REQUIRED_HEADINGS = (
    "## Task Definition",
    "## Context",
    "## Operating Protocol",
    "## File Contract",
    "## Quality Standard",
    "## Tools",
    "## Examples",
)
NEGATIVE_DIRECTIVE = re.compile(
    r"\b(?:do not|don't|must not|never|cannot|can't|without|avoid)\b",
    flags=re.IGNORECASE,
)
INLINE_EXAMPLE = re.compile(r"\b(?:for example|e\.g\.|such as)\b", flags=re.IGNORECASE)
MAX_DIRECTIVE_WORDS = 30


def _style_errors(stage: str, text: str) -> list[str]:
    errors: list[str] = []
    heading_positions = [text.find(heading) for heading in REQUIRED_HEADINGS]
    if all(position >= 0 for position in heading_positions) and heading_positions != sorted(heading_positions):
        errors.append(f"{stage}: Prompt headings are out of order")
    core = text.split("## Examples", 1)[0]
    for number, line in enumerate(core.splitlines(), start=1):
        directive = line.strip().lstrip("-0123456789. ")
        if not directive or directive.startswith("#"):
            continue
        if NEGATIVE_DIRECTIVE.search(directive):
            errors.append(f"{stage}: line {number} uses a negative directive")
        if INLINE_EXAMPLE.search(directive):
            errors.append(f"{stage}: line {number} mixes an example into a core directive")
        if len(directive.split()) > MAX_DIRECTIVE_WORDS:
            errors.append(f"{stage}: line {number} exceeds {MAX_DIRECTIVE_WORDS} words")
    return errors


def check_prompt_contracts(builder_root: str | Path) -> dict[str, Any]:
    root = Path(builder_root)
    errors: list[str] = []
    for stage, directory in PROMPT_FOR_STAGE.items():
        path = root / "subagents" / directory / "AGENT.md"
        if not path.is_file():
            errors.append(f"{stage}: missing Prompt {path}")
            continue
        text = path.read_text(encoding="utf-8")
        for heading in REQUIRED_HEADINGS:
            if heading not in text:
                errors.append(f"{stage}: missing heading {heading}")
        errors.extend(_style_errors(stage, text))
        protocol = STAGE_PROTOCOLS[stage]
        for field in (
            *INPUT_FIELDS[stage],
            *(protocol.model_fields or protocol.handoff_fields),
            *protocol.prompt_terms,
        ):
            if f"`{field}`" not in text:
                errors.append(f"{stage}: Prompt does not declare `{field}`")
    if errors:
        raise ContractError("Prompt and Validator contracts are not aligned", errors=errors)
    return {"ok": True, "stages": sorted(PROMPT_FOR_STAGE), "errors": []}
