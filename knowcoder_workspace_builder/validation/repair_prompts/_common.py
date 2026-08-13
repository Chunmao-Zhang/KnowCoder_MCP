from __future__ import annotations

from typing import Any, Callable

MatchFn = Callable[[list[str], dict[str, Any]], bool]


def joined(errors: list[str]) -> str:
    return "\n".join(str(item) for item in errors).casefold()


def contains(*needles: str) -> MatchFn:
    lowered = tuple(item.casefold() for item in needles)

    def matcher(errors: list[str], _context: dict[str, Any]) -> bool:
        text = joined(errors)
        return any(needle in text for needle in lowered)

    return matcher


def contains_all(*needles: str) -> MatchFn:
    lowered = tuple(item.casefold() for item in needles)

    def matcher(errors: list[str], _context: dict[str, Any]) -> bool:
        text = joined(errors)
        return all(needle in text for needle in lowered)

    return matcher


def render(
    *,
    stage: str,
    mode: str,
    case_name: str,
    hint: str,
    errors: list[str],
    context: dict[str, Any],
) -> str:
    lines = [
        "Read the saved candidate or unit payload when available.",
        "Prefer local corrections over regenerating the entire payload.",
        f"Stage: {stage}",
        f"Validation mode: {mode}",
        f"Matched repair case: {case_name}",
        hint,
    ]
    if errors:
        lines.extend(["", "Validation errors:"])
        lines.extend(f"- {item}" for item in errors[:8])
    extras: list[str] = []
    if context.get("repair_hint"):
        extras.append(str(context["repair_hint"]))
    for key, label in [
        ("allowed_entity_types", "Allowed entity types"),
        ("allowed_attributes", "Allowed attributes"),
        ("allowed_source_ids", "Allowed source IDs"),
        ("missing", "Missing source IDs"),
    ]:
        if context.get(key):
            extras.append(f"{label}: " + ", ".join(map(str, context[key])))
    if context.get("allowed_relations"):
        relations = context["allowed_relations"]
        rendered = []
        if isinstance(relations, list):
            for item in relations[:12]:
                if isinstance(item, dict):
                    rendered.append(f"{item.get('name')}({item.get('head')}->{item.get('tail')})")
                else:
                    rendered.append(str(item))
        if rendered:
            extras.append("Allowed relations: " + ", ".join(rendered))
    if extras:
        lines.extend(["", "Context:"])
        lines.extend(f"- {item}" for item in extras)
    return "\n".join(lines).strip() + "\n"


def resolve_cases(
    stage: str,
    mode: str,
    cases: list[tuple[str, MatchFn, str]],
    *,
    errors: list[str] | None,
    context: dict[str, Any] | None,
) -> str:
    error_list = [str(item) for item in (errors or []) if str(item).strip()]
    ctx = dict(context or {})
    selected = ("default", "Prefer local corrections over full regeneration.")
    for name, matcher, hint in cases:
        try:
            matched = matcher(error_list, ctx)
        except Exception:
            matched = False
        if matched:
            selected = (name, hint)
            break
    return render(
        stage=stage,
        mode=mode,
        case_name=selected[0],
        hint=selected[1],
        errors=error_list,
        context=ctx,
    )
