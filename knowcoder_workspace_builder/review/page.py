"""Generate self-contained, durable Problem and Schema Review pages."""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any

from knowcoder_workspace_builder.storage.locks import SessionLockStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout, validate_session_id
from knowcoder_workspace_builder.storage.transaction import AtomicWriter


REVIEW_STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
REVIEW_STYLESHEET = REVIEW_STATIC_ROOT / "reviewer.css"


def _humanize(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"^Name(?=[A-Z])", "", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[_-]+", " ", text).strip()
    return text[:1].upper() + text[1:]


def _problem_content(review: dict[str, Any]) -> str:
    question = escape(str(review.get("question") or review.get("confirmed_problem") or ""))
    raw_steps = review.get("workflow_steps")
    steps = raw_steps if isinstance(raw_steps, list) else []
    rows = "".join(
        f'<article class="step-row readonly-row"><div class="step-track"><span class="step-index">{index}</span></div>'
        f'<div class="step-content"><span class="step-label">Research step</span>'
        f'<p>{escape(str(step.get("description") or step.get("title") or ""))}</p></div></article>'
        for index, step in enumerate(steps, start=1)
        if isinstance(step, dict)
    )
    return f"""
      <div class="review-workbench problem-workbench">
        <div class="review-scroll-region problem-scroll-region">
          <section class="review-panel question-panel">
            <div class="panel-title-row">
              <div><span class="panel-kicker">Research brief</span><h2>Confirmed scope</h2><p class="panel-description">The complete question that guides evidence collection.</p></div>
              <span class="panel-count">{len(steps)} steps</span>
            </div>
            <div class="question-body readonly-copy">{question}</div>
          </section>
          <section class="review-panel steps-panel">
            <div class="panel-title-row">
              <div><span class="panel-kicker">Research plan</span><h2>Investigation steps</h2><p class="panel-description">The sequence used to collect and organize evidence.</p></div>
            </div>
            <div class="step-list">{rows}</div>
          </section>
        </div>
      </div>"""


def _entity_card(entity: dict[str, Any], index: int) -> str:
    raw_fields = entity.get("fields")
    fields = raw_fields if isinstance(raw_fields, list) else []
    name = str(entity.get("name") or "")
    field_rows = "".join(
        f'<div class="field-row readonly-row">'
        f'<span class="attribute-name"><strong>{escape(_humanize(field.get("name")))}</strong><code>{escape(str(field.get("name") or ""))}</code></span>'
        f'<span><code class="type-chip">{escape(str(field.get("type") or ""))}</code></span>'
        f'<span><span class="requirement-chip {"optional" if field.get("optional") else "required"}">'
        f'{"Optional" if field.get("optional") else "Required"}</span></span></div>'
        for field in fields
        if isinstance(field, dict)
    )
    return f"""
      <article class="entity-card">
        <div class="schema-card-head readonly-schema-head">
          <span class="schema-card-index">E{index}</span>
          <div class="schema-name-field"><strong>{escape(_humanize(name))}</strong><code>{escape(name)}</code></div>
          <div class="schema-desc-field"><span>Description</span><p>{escape(str(entity.get("description") or ""))}</p></div>
        </div>
        <div class="field-table">
          <div class="field-table-head"><span>Attribute</span><span>Data type</span><span>Required</span></div>
          {field_rows}
        </div>
      </article>"""


def _relation_card(relation: dict[str, Any], index: int) -> str:
    name = str(relation.get("name") or "")
    return f"""
      <article class="relation-card">
        <div class="relation-card-head">
          <span class="schema-card-index">R{index}</span>
          <div class="schema-name-field"><strong>{escape(_humanize(name))}</strong><code>{escape(name)}</code></div>
        </div>
        <div class="relation-endpoints">
          <span class="endpoint-chip">{escape(_humanize(relation.get("head")))}</span><span class="relation-arrow">→</span><span class="endpoint-chip">{escape(_humanize(relation.get("tail")))}</span>
        </div>
        <p class="relation-description">{escape(str(relation.get("description") or ""))}</p>
      </article>"""


def _schema_content(review: dict[str, Any]) -> str:
    raw_entities = review.get("entities")
    raw_relations = review.get("relations")
    entities = raw_entities if isinstance(raw_entities, list) else []
    relations = raw_relations if isinstance(raw_relations, list) else []
    field_count = sum(
        len(entity.get("fields"))
        for entity in entities
        if isinstance(entity, dict) and isinstance(entity.get("fields"), list)
    )
    entity_cards = "".join(
        _entity_card(entity, index)
        for index, entity in enumerate(entities, start=1)
        if isinstance(entity, dict)
    )
    relation_cards = "".join(
        _relation_card(relation, index)
        for index, relation in enumerate(relations, start=1)
        if isinstance(relation, dict)
    )
    return f"""
      <div class="review-workbench schema-workbench">
        <div class="review-scroll-region schema-scroll-region">
          <section class="schema-summary-strip" aria-label="Schema summary">
            <div class="schema-summary-item"><span>Entity types</span><strong>{len(entities)}</strong></div>
            <div class="schema-summary-item"><span>Attributes</span><strong>{field_count}</strong></div>
            <div class="schema-summary-item"><span>Relations</span><strong>{len(relations)}</strong></div>
          </section>
          <section class="review-panel entity-panel">
            <div class="panel-title-row"><div><span class="panel-kicker">Knowledge model</span><h2>Entity types</h2><p class="panel-description">The concepts and attributes that will organize the collected evidence.</p></div></div>
            <div class="entity-list">{entity_cards}</div>
          </section>
          <section class="review-panel relation-panel">
            <div class="panel-title-row"><div><span class="panel-kicker">Knowledge graph</span><h2>Relations</h2><p class="panel-description">How the entity types connect to one another.</p></div></div>
            <div class="relation-list">{relation_cards}</div>
          </section>
        </div>
      </div>"""


def render_review_page(workspace_id: str, review_type: str, review: dict[str, Any]) -> str:
    """Render one complete page with no server, script, or external asset dependency."""
    if review_type not in {"problem", "schema"}:
        raise ValueError("Review type must be problem or schema")
    safe_workspace_id = escape(validate_session_id(workspace_id), quote=True)
    label = "Problem" if review_type == "problem" else "Schema"
    subtitle = (
        "Check the research scope and investigation plan."
        if review_type == "problem"
        else "Check the knowledge model before extraction begins."
    )
    content = _problem_content(review) if review_type == "problem" else _schema_content(review)
    stylesheet = REVIEW_STYLESHEET.read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{label} Review · KnowCoder</title>
  <style>{stylesheet}</style>
</head>
<body data-workspace-id="{safe_workspace_id}" data-review-type="{review_type}">
  <main class="review-shell">
    <header class="review-header">
      <div>
        <span class="eyebrow">Workspace Builder Review</span>
        <h1>{label} Review</h1>
        <p id="review-subtitle">{subtitle}</p>
      </div>
      <div class="header-actions"><span class="review-state-badge confirmed">Read only</span></div>
    </header>
    <section class="review-card">{content}</section>
  </main>
</body>
</html>"""


def write_review_page(
    layout: ProjectLayout,
    workspace_id: str,
    review_type: str,
    version: int,
    review: dict[str, Any],
) -> Path:
    """Persist an immutable Review version inside its Workspace."""
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Review version must be a positive integer")
    paths = layout.session(workspace_id)
    target = paths.workspace / "review" / f"{review_type}-v{version}.html"
    # The accepted version is the snapshot identity. Several MCP host
    # processes can discover the same Review concurrently, and processes
    # started before a presentation update may render different HTML for the
    # same accepted data. Reuse the first durable snapshot instead of treating
    # a process-local presentation difference as a workflow failure.
    with SessionLockStore(layout).acquire(workspace_id):
        if target.is_file():
            return target
        content = render_review_page(workspace_id, review_type, review)
        return AtomicWriter(paths).text(target, content)
