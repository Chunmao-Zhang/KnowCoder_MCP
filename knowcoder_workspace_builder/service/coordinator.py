"""Advance accepted Builder stages without performing specialist work."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from knowcoder_workspace_builder.contracts.errors import (
    TRANSIENT_EXTERNAL_ERROR_TYPES,
    BuilderError,
    ContractError,
    MissingStateError,
    StateConflictError,
)
from knowcoder_workspace_builder.runtime.retry_policy import wait_before_retry
from knowcoder_workspace_builder.storage.events import EventStore
from knowcoder_workspace_builder.storage.canonical import canonical_index
from knowcoder_workspace_builder.storage.canonical import canonical_artifact_path
from knowcoder_workspace_builder.storage.extensions import workspace_catalog
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.runtime.virtual_paths import virtual_session_path
from knowcoder_workspace_builder.runtime.timeouts import DEFAULT_TRANSIENT_RETRY_LIMIT
from knowcoder_workspace_builder.workflow.models import BuildState
from knowcoder_workspace_builder.workflow.sources import split_sources
from knowcoder_workspace_builder.storage.instances import validate_instances
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.sources import SourceRepository
from knowcoder_workspace_builder.storage.stage_artifacts import merge_final_drafts
from knowcoder_workspace_builder.storage.transaction import read_json
from knowcoder_workspace_builder.workflow.stages import AGENT_FOR_STAGE, Stage

from .finalize import finalize_workspace
from .stage_runner import StageRunner


def _compact_text(value: object, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_extraction_sources(sources: list[object]) -> list[dict[str, object]]:
    """Keep only the fields extractors need; drop search metadata that bloats prompts."""
    compact: list[dict[str, object]] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "").strip()
        file_path = str(item.get("file_path") or "").strip()
        if not source_id or not file_path:
            continue
        compact.append(
            {
                "source_id": source_id,
                "file_path": file_path,
                "source_kind": str(item.get("source_kind") or ""),
                "title": _compact_text(item.get("title"), limit=120),
            }
        )
    return compact


def schema_data_manifest(evidence: dict[str, object]) -> dict[str, object]:
    """Project accepted data into the compact contract needed for schema design."""
    raw_coverage = evidence.get("coverage")
    coverage = raw_coverage if isinstance(raw_coverage, list) else []
    projected_coverage: list[dict[str, object]] = []
    for index, item in enumerate(coverage, start=1):
        if not isinstance(item, dict):
            continue
        requirements = item.get("requirements")
        compact_requirements: list[str] = []
        if isinstance(requirements, list):
            for requirement in requirements[:8]:
                text = _compact_text(requirement, limit=120)
                if text:
                    compact_requirements.append(text)
        projected_coverage.append(
            {
                "step_index": index,
                "requirements": compact_requirements,
                "status": str(item.get("status") or ""),
            }
        )
    unresolved = evidence.get("unresolved_gaps")
    compact_unresolved = [
        _compact_text(item, limit=120)
        for item in (unresolved if isinstance(unresolved, list) else [])
        if str(item or "").strip()
    ][:12]
    return {
        "coverage": projected_coverage,
        "unresolved_gaps": compact_unresolved,
    }


def _already_processed_source_ids(state: BuildState, bucket: str) -> list[str]:
    extraction = state.extraction if isinstance(state.extraction, dict) else {}
    record = extraction.get(bucket) if isinstance(extraction.get(bucket), dict) else {}
    if str(record.get("status") or "") == "skipped":
        return []
    raw = record.get("processed_source_ids")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        source_id = str(item or "").strip()
        if source_id and source_id not in seen:
            seen.add(source_id)
            ordered.append(source_id)
    return ordered


def _sources_excluding(sources: list[dict[str, object]], processed_ids: list[str]) -> list[dict[str, object]]:
    blocked = set(processed_ids)
    if not blocked:
        return sources
    return [
        item
        for item in sources
        if str(item.get("source_id") or "").strip() not in blocked
    ]


def _coverage_step_index(item: dict[str, object], fallback: int) -> int:
    value = item.get("step_index")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback


def _affected_extraction_step_indexes(state: BuildState, steps: list[object]) -> list[int] | None:
    """Return targeted step indexes, or None when the whole question needs extraction."""
    valid_indexes = set(range(1, len(steps) + 1))
    requested = [
        index
        for index in state.pending_evidence_step_indexes
        if isinstance(index, int) and not isinstance(index, bool) and index in valid_indexes
    ]
    if requested:
        return list(dict.fromkeys(requested))
    if state.workspace_mode != "extend" or not state.extension_baseline_steps:
        return None

    baseline = [str(step).strip() for step in state.extension_baseline_steps if str(step).strip()]
    current = [str(step).strip() for step in steps]
    changed = [
        index
        for index, (old_step, current_step) in enumerate(zip(baseline, current), start=1)
        if old_step != current_step
    ]
    changed.extend(range(len(baseline) + 1, len(current) + 1))
    return changed


def _sources_for_step_indexes(
    evidence: dict[str, object],
    sources: list[dict[str, object]],
    step_indexes: list[int],
) -> list[dict[str, object]]:
    selected_indexes = set(step_indexes)
    selected_source_ids: set[str] = set()
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), list) else []
    for fallback, item in enumerate(coverage, start=1):
        if not isinstance(item, dict) or _coverage_step_index(item, fallback) not in selected_indexes:
            continue
        for value in item.get("source_ids") or []:
            source_id = str(value or "").strip()
            if source_id:
                selected_source_ids.add(source_id)
    return [
        source
        for source in sources
        if str(source.get("source_id") or "").strip() in selected_source_ids
    ]


def _extraction_units_for_sources(
    evidence: dict[str, object],
    assigned_sources: list[dict[str, object]],
    step_indexes: list[int] | None = None,
) -> list[dict[str, object]]:
    """Build one extraction unit per selected source chunk.

    Research-step metadata stays attached to every unit, while each model
    invocation receives exactly one chunk. Sources without chunk bindings stay
    explicit as one source-level unit so uploaded or legacy evidence is never
    silently dropped.
    """
    assigned_ids = {
        str(item.get("source_id") or "").strip()
        for item in assigned_sources
        if str(item.get("source_id") or "").strip()
    }
    if not assigned_ids:
        return []
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), list) else []
    units: list[dict[str, object]] = []
    claimed: set[str] = set()
    selected_indexes = set(step_indexes) if step_indexes is not None else None
    for fallback, item in enumerate(coverage, start=1):
        if not isinstance(item, dict):
            continue
        index = _coverage_step_index(item, fallback)
        if selected_indexes is not None and index not in selected_indexes:
            continue
        raw_ids = item.get("source_ids") if isinstance(item.get("source_ids"), list) else []
        step_ids = [
            source_id
            for source_id in (str(value).strip() for value in raw_ids)
            if source_id and source_id in assigned_ids
        ]
        # Keep order, drop duplicates.
        ordered: list[str] = []
        seen: set[str] = set()
        for source_id in step_ids:
            if source_id not in seen:
                seen.add(source_id)
                ordered.append(source_id)
        if not ordered:
            continue
        claimed.update(ordered)
        step = str(item.get("step") or "").strip()
        requirements = (
            list(item.get("requirements") or [])[:8]
            if isinstance(item.get("requirements"), list)
            else []
        )
        refs: list[dict[str, str]] = []
        seen_refs: set[tuple[str, str]] = set()
        for ref in item.get("chunk_refs") or []:
            if not isinstance(ref, dict):
                continue
            source_id = str(ref.get("source_id") or "").strip()
            chunk_id = str(ref.get("chunk_id") or "").strip()
            key = (source_id, chunk_id)
            if source_id not in ordered or not chunk_id or key in seen_refs:
                continue
            seen_refs.add(key)
            refs.append({"source_id": source_id, "chunk_id": chunk_id})
        referenced_sources = {ref["source_id"] for ref in refs}
        for ref in refs:
            units.append(
                {
                    "step_index": index,
                    "step": step,
                    "source_ids": [ref["source_id"]],
                    "chunk_refs": [ref],
                    "requirements": requirements,
                }
            )
        for source_id in ordered:
            if source_id in referenced_sources:
                continue
            units.append(
                {
                    "step_index": index,
                    "step": step,
                    "source_ids": [source_id],
                    "chunk_refs": [],
                    "requirements": requirements,
                }
            )
    leftovers = sorted(assigned_ids - claimed)
    if leftovers:
        fallback_step = len(coverage) + 1 if coverage else 1
        units.extend(
            {
                "step_index": fallback_step,
                "step": "Remaining assigned sources",
                "source_ids": [source_id],
                "chunk_refs": [],
                "requirements": [],
            }
            for source_id in leftovers
        )
    if not units:
        units.extend(
            {
                "step_index": 1,
                "step": "All assigned sources",
                "source_ids": [source_id],
                "chunk_refs": [],
                "requirements": [],
            }
            for source_id in sorted(assigned_ids)
        )
    for unit_index, unit in enumerate(units, start=1):
        unit["unit_index"] = unit_index
    return units


def _uncovered_step_indexes(steps: list[object], evidence: object) -> list[int]:
    accepted_steps: set[str] = set()
    if isinstance(evidence, dict) and isinstance(evidence.get("coverage"), list):
        formal_source_ids = {
            str(source.get("source_id") or "").strip()
            for source in evidence.get("sources") or []
            if isinstance(source, dict)
            and str(source.get("source_kind") or "").strip()
            not in {"web", "web_search", "web_search_bundle", "web_search_result"}
        }
        for item in evidence["coverage"]:
            if not isinstance(item, dict) or not isinstance(item.get("source_ids"), list):
                continue
            if not any(str(source_id).strip() in formal_source_ids for source_id in item["source_ids"]):
                continue
            step = str(item.get("step") or "").strip()
            if step:
                accepted_steps.add(step)
    return [
        index
        for index, step in enumerate(steps, start=1)
        if str(step).strip() not in accepted_steps
    ]


class Coordinator:
    def __init__(
        self,
        layout: ProjectLayout,
        stage_runner: StageRunner,
        *,
        schema_revision_limit: int = 5,
        transient_retry_limit: int = DEFAULT_TRANSIENT_RETRY_LIMIT,
    ) -> None:
        if schema_revision_limit < 1:
            raise ValueError("Schema revision limit must be positive")
        if transient_retry_limit < 0:
            raise ValueError("Transient retry limit cannot be negative")
        self.layout = layout
        self.stage_runner = stage_runner
        self.states = BuildStateStore(layout)
        self.events = EventStore(layout)
        self.schema_revision_limit = schema_revision_limit
        self.transient_retry_limit = transient_retry_limit

    def run_until_gate(self, state: BuildState, *, turn_id: str = "") -> BuildState:
        if state.status != "running":
            raise StateConflictError("Builder can resume only from running status", status=state.status)
        transient_retries: dict[Stage, int] = {}
        while state.status == "running":
            stage = Stage(state.stage)
            if stage == Stage.FINALIZE:
                return self._finalize(state)
            if stage == Stage.READY:
                return state
            if stage == Stage.SCHEMA_BUILD and state.schema_revision_rounds >= self.schema_revision_limit:
                return self._fail_revision_limit(state)
            try:
                stage_input = self._stage_input(state, stage)
                state = self.stage_runner.run(state, stage_input, turn_id=turn_id)
            except (ContractError, MissingStateError) as exc:
                # Pre-attempt contract failures otherwise leave the persisted Builder in
                # running state even though the host Solver has already stopped. StageRunner
                # owns failures after an attempt starts; this branch closes only failures
                # that occur before an attempt is active.
                current = self.states.load(state.session_id)
                if current.status != "running" or current.active_attempt_id:
                    raise
                state = self._fail_preflight(current, stage, exc, turn_id)
            if state.status == "failed" and self._is_transient_failure(state.failure):
                retry_number = transient_retries.get(stage, 0) + 1
                if retry_number <= self.transient_retry_limit:
                    transient_retries[stage] = retry_number
                    wait_before_retry(retry_number)
                    state = self._retry_transient(state, stage, retry_number, turn_id)
        return state

    @staticmethod
    def _is_transient_failure(failure: object) -> bool:
        if not isinstance(failure, dict):
            return False
        if failure.get("code") in {"external_service_error", "invocation_timeout"}:
            return True
        context = failure.get("context")
        error_type = str(context.get("error_type") or "") if isinstance(context, dict) else ""
        return error_type in TRANSIENT_EXTERNAL_ERROR_TYPES

    def _retry_transient(
        self,
        state: BuildState,
        stage: Stage,
        retry_number: int,
        turn_id: str,
    ) -> BuildState:
        error = dict(state.failure or {})

        def resume(current: BuildState) -> BuildState:
            if current.status != "failed" or current.stage != stage:
                raise StateConflictError(
                    "Builder state changed before transient retry",
                    expected_stage=stage,
                    actual_stage=current.stage,
                    status=current.status,
                )
            current.status = "running"
            current.active_attempt_id = ""
            current.failure = None
            return current

        retried = self.states.update(state.session_id, state.version, resume)
        self.events.append(
            state.session_id,
            kind="stage_retry",
            status="running",
            stage=stage,
            agent=AGENT_FOR_STAGE[stage],
            turn_id=turn_id,
            report="Retrying the current stage after a transient external service failure.",
            public_data={"retry_number": retry_number, "previous_error": error},
        )
        return retried

    def _stage_input(self, state: BuildState, stage: Stage) -> dict[str, object]:
        if stage == Stage.PROBLEM:
            paths = self.layout.session(state.session_id)
            catalog = workspace_catalog(self.layout)
            completed = all(
                (paths.workspace / name).is_file()
                for name in (
                    "README.md",
                    "ontology/types.py",
                    "ontology/schema.json",
                    "ontology/loader.py",
                    "data/entities.jsonl",
                    "data/relations.jsonl",
                    "data/manifest.json",
                )
            )
            prior_problem = dict(state.problem or {})
            revision_instruction = str(
                state.pending_revision or state.active_follow_up_request or ""
            ).strip()
            baseline_steps = prior_problem.get("steps")
            if not isinstance(baseline_steps, list):
                baseline_steps = []
            in_place_extension = completed and state.invalidated_from == Stage.PROBLEM
            required_base_workspace_id = state.session_id if in_place_extension else ""
            if in_place_extension:
                manifest = read_json(paths.workspace / "data" / "manifest.json")
                if not isinstance(manifest, dict):
                    raise ContractError("Completed Workspace manifest must be an object")
                readme = (paths.workspace / "README.md").read_text(encoding="utf-8")
                # An in-place Follow-up has exactly one legal baseline: this Session's
                # completed Workspace. Keeping unrelated catalog entries out also avoids
                # prompt bloat and prevents the model from selecting a similar Session.
                catalog = [
                    {
                        "workspace_id": state.session_id,
                        "updated_at": state.updated_at,
                        "question": state.question,
                        "readme_path": (paths.workspace / "README.md").relative_to(self.layout.project).as_posix(),
                        "readme": readme,
                        "problem_steps": [str(step) for step in baseline_steps if str(step).strip()],
                        "records": dict(manifest.get("records") or {}),
                    }
                ]
            mode = "extend" if in_place_extension else "revise" if revision_instruction and prior_problem else "new"
            return {
                "question": state.question,
                "upload_paths": list(state.upload_paths),
                "current_date": datetime.now().astimezone().date().isoformat(),
                "workspace_context": {
                    "mode": mode,
                    "required_base_workspace_id": required_base_workspace_id,
                    "prior_problem": prior_problem or None,
                    "baseline_steps": [str(step) for step in baseline_steps if str(step).strip()],
                    "revision_instruction": revision_instruction,
                    "follow_up_request": state.active_follow_up_request,
                    "retry_reason": state.retry_reason,
                    "workspace_catalog": catalog,
                    "workspace_readme": (paths.workspace / "README.md").read_text(encoding="utf-8")
                    if (paths.workspace / "README.md").is_file()
                    else "",
                    "artifact_index": canonical_index(paths),
                },
            }

        problem = state.problem or {}
        steps = problem.get("steps")
        if not isinstance(steps, list) or not steps:
            raise MissingStateError("Confirmed problem steps are missing", session_id=state.session_id)
        if stage == Stage.EVIDENCE:
            accepted_sources = (
                state.evidence.get("sources")
                if isinstance(state.evidence, dict) and isinstance(state.evidence.get("sources"), list)
                else []
            )
            required_source_ids = [
                str(item.get("source_id") or "").strip()
                for item in accepted_sources
                if isinstance(item, dict)
                and str(item.get("source_kind") or "") == "upload"
                and str(item.get("source_id") or "").strip()
            ]
            attached_paths = {str(item).strip() for item in state.upload_paths if str(item).strip()}
            for item in SourceRepository(self.layout.session(state.session_id)).list():
                source_id = str(item.get("source_id") or "").strip()
                file_path = str(item.get("file_path") or "").strip()
                if (
                    str(item.get("source_kind") or "") == "upload"
                    and file_path in attached_paths
                    and source_id not in required_source_ids
                ):
                    required_source_ids.append(source_id)
            return {
                "question": state.question,
                "steps": steps,
                "upload_paths": list(state.upload_paths),
                "research_dir": virtual_session_path("intermediate"),
                "workspace_context": {
                    **self._workspace_context(state),
                    "mode": "revise" if state.evidence else "new",
                    "accepted_data": state.evidence,
                    "required_source_ids": required_source_ids,
                    "revision_instruction": state.pending_revision or state.active_follow_up_request,
                    "uncovered_step_indexes": list(state.pending_evidence_step_indexes)
                    or _uncovered_step_indexes(steps, state.evidence),
                },
            }

        evidence = state.evidence
        if not isinstance(evidence, dict):
            raise MissingStateError("Accepted evidence is missing", session_id=state.session_id)
        schema_manifest = schema_data_manifest(evidence)
        if stage == Stage.SCHEMA_BUILD:
            judgement = (state.schema_review or {}).get("judgement") or {}
            compact_steps = [_compact_text(step, limit=180) for step in steps if str(step or "").strip()]
            schema_step_indexes = list(range(1, len(compact_steps) + 1))
            revision_requirements = judgement.get("missing_requirements") or []
            if not isinstance(revision_requirements, list):
                revision_requirements = []
            compact_requirements = [
                _compact_text(item, limit=160) for item in revision_requirements if str(item or "").strip()
            ][:20]
            user_instruction = str(state.pending_revision or "").strip()
            # Promote free-text user revision into the same first-class list the model
            # already prioritizes, so rename/remove instructions are not ignored.
            if user_instruction and user_instruction not in compact_requirements:
                compact_requirements = [user_instruction, *compact_requirements][:20]
            if compact_requirements:
                compact_steps = ["Apply all listed revision requirements to the accumulated Schema."]
                schema_step_indexes = [0]
            elif state.workspace_mode == "extend" and state.pending_evidence_step_indexes:
                valid_indexes = set(range(1, len(compact_steps) + 1))
                schema_step_indexes = list(
                    dict.fromkeys(
                        index
                        for index in state.pending_evidence_step_indexes
                        if isinstance(index, int)
                        and not isinstance(index, bool)
                        and index in valid_indexes
                    )
                )
                if not schema_step_indexes:
                    raise ContractError(
                        "Extension evidence step indexes do not match the confirmed research steps",
                        requested_step_indexes=state.pending_evidence_step_indexes,
                        step_count=len(compact_steps),
                    )
                compact_steps = [compact_steps[index - 1] for index in schema_step_indexes]
            elif state.workspace_mode == "extend" and state.base_workspace_id:
                if state.base_workspace_id == state.session_id:
                    baseline_steps = [
                        str(step).strip()
                        for step in state.extension_baseline_steps
                        if str(step).strip()
                    ]
                else:
                    baseline = self.states.load(state.base_workspace_id)
                    baseline_problem = baseline.problem if isinstance(baseline.problem, dict) else {}
                    baseline_steps = [
                        str(step).strip()
                        for step in (baseline_problem.get("steps") or [])
                        if str(step).strip()
                    ]
                current_steps = [str(step).strip() for step in steps if str(step).strip()]
                if not baseline_steps:
                    raise MissingStateError(
                        "Extension baseline problem steps are missing",
                        workspace_id=state.base_workspace_id,
                    )
                affected_indexes = [
                    index
                    for index, (baseline_step, current_step) in enumerate(
                        zip(baseline_steps, current_steps),
                        start=1,
                    )
                    if baseline_step != current_step
                ]
                affected_indexes.extend(range(len(baseline_steps) + 1, len(current_steps) + 1))
                if affected_indexes:
                    compact_steps = [
                        _compact_text(current_steps[index - 1], limit=180)
                        for index in affected_indexes
                    ]
                    schema_step_indexes = affected_indexes
                else:
                    compact_steps = ["Confirm the accumulated Schema covers the unchanged extension request."]
                    schema_step_indexes = [0]
            return {
                "question": _compact_text(state.question, limit=400),
                "steps": compact_steps,
                "schema_step_indexes": schema_step_indexes,
                "data_manifest": schema_manifest,
                "workspace_context": {
                    **self._workspace_context(state),
                    "mode": "revise" if state.schema_review or user_instruction else "new",
                    "current_schema": (state.schema_review or {}).get("schema_source"),
                    "revision_requirements": compact_requirements,
                    "user_instruction": user_instruction,
                },
            }

        schema_review = state.schema_review
        if not isinstance(schema_review, dict) or not str(schema_review.get("schema_source") or "").strip():
            raise MissingStateError("Current schema candidate is missing", session_id=state.session_id)
        if stage == Stage.SCHEMA_JUDGE:
            user_instruction = str(state.pending_revision or "").strip()
            mode = (
                "review_edit"
                if user_instruction and not state.invalidated_from
                else "follow_up" if user_instruction else "standard"
            )
            return {
                "question": state.question,
                "steps": steps,
                "data_manifest": schema_manifest,
                "schema_source": schema_review["schema_source"],
                "workspace_context": {
                    **self._workspace_context(state),
                    "mode": mode,
                    "user_instruction": user_instruction,
                },
            }

        sources = evidence.get("sources")
        if not isinstance(sources, list):
            raise MissingStateError("Accepted evidence sources are missing", session_id=state.session_id)
        stored_sources = {
            str(item.get("source_id") or ""): item
            for item in SourceRepository(self.layout.session(state.session_id)).list()
            if str(item.get("source_id") or "")
        }
        sources = [
            item
            for item in sources
            if not isinstance(item, dict)
            or stored_sources.get(str(item.get("source_id") or ""), {}).get("status") != "superseded"
        ]
        source_split = split_sources(sources)
        if stage == Stage.EXTRACT:
            assigned = _compact_extraction_sources(source_split["unstructured"])
            affected_step_indexes = _affected_extraction_step_indexes(state, steps)
            if affected_step_indexes is not None:
                assigned = _sources_for_step_indexes(evidence, assigned, affected_step_indexes)
            already = _already_processed_source_ids(state, "unstructured")
            if state.workspace_mode == "extend" and state.extension_baseline_steps:
                already = []
            remaining = _sources_excluding(assigned, already)
            extraction_units = _extraction_units_for_sources(
                evidence if isinstance(evidence, dict) else {},
                remaining,
                affected_step_indexes,
            )
            return {
                "schema_outline": schema_review.get("schema_outline") or {},
                "sources": remaining,
                "workspace_context": {
                    **self._workspace_context(state),
                    "revision_instruction": state.pending_revision or state.active_follow_up_request,
                    "invalidated_from": state.invalidated_from,
                    "already_processed_source_ids": already,
                    "confirmed_steps": steps,
                    "confirmed_requirements": schema_manifest["coverage"],
                    "extraction_units": extraction_units,
                },
            }
        if stage == Stage.STRUCTURED_EXTRACT:
            assigned = _compact_extraction_sources(source_split["structured"])
            affected_step_indexes = _affected_extraction_step_indexes(state, steps)
            if affected_step_indexes is not None:
                assigned = _sources_for_step_indexes(evidence, assigned, affected_step_indexes)
            already = _already_processed_source_ids(state, "structured")
            if state.workspace_mode == "extend" and state.extension_baseline_steps:
                already = []
            remaining = _sources_excluding(assigned, already)
            return {
                "schema_outline": schema_review.get("schema_outline") or {},
                "sources": remaining,
                "work_dir": virtual_session_path("intermediate/sources"),
                "workspace_context": {
                    **self._workspace_context(state),
                    "revision_instruction": state.pending_revision or state.active_follow_up_request,
                    "invalidated_from": state.invalidated_from,
                    "already_processed_source_ids": already,
                    "confirmed_steps": steps,
                    "confirmed_requirements": schema_manifest["coverage"],
                },
            }
        if stage == Stage.DOCUMENT:
            draft_paths = self._accepted_draft_paths(state)
            instances = merge_final_drafts([read_json(path) for path in draft_paths])
            validate_instances(instances, parse_schema(str(schema_review.get("schema_source") or "")))
            return {
                "problem": dict(problem),
                "schema_source": str(schema_review.get("schema_source") or ""),
                "instance_summary": {
                    "entity_count": len(instances["entities"]),
                    "relation_count": len(instances["relations"]),
                },
                "sources": [
                    {
                        "source_id": str(item.get("source_id") or ""),
                        "title": str(item.get("title") or item.get("source_id") or ""),
                        "source_kind": str(item.get("source_kind") or ""),
                    }
                    for item in sources
                    if isinstance(item, dict)
                ],
                "artifact_index": canonical_index(self.layout.session(state.session_id)),
                "workspace_context": {
                    **self._workspace_context(state),
                    "revision_instruction": state.pending_revision or state.active_follow_up_request,
                    "invalidated_from": state.invalidated_from,
                },
            }
        raise ContractError("Coordinator cannot build input for stage", stage=stage)

    def _workspace_context(self, state: BuildState) -> dict[str, object]:
        paths = self.layout.session(state.session_id)
        readme = paths.workspace / "README.md"
        return {
            "workspace_mode": state.workspace_mode,
            "base_workspace_id": state.base_workspace_id,
            "follow_up_request": state.active_follow_up_request,
            "retry_reason": state.retry_reason,
            "workspace_readme": readme.read_text(encoding="utf-8") if readme.is_file() else "",
            "artifact_index": canonical_index(paths),
        }

    def _accepted_draft_paths(self, state: BuildState) -> list[Path]:
        paths = self.layout.session(state.session_id)
        candidates = [] if state.replace_instances else [paths.stages / "baseline" / "instances.json"]
        for stage, artifact in (
            (Stage.EXTRACT, "unstructured_draft.json"),
            (Stage.STRUCTURED_EXTRACT, "structured_draft.json"),
        ):
            attempt_id = state.accepted_attempts.get(str(stage), "")
            if attempt_id:
                candidates.append(paths.attempts / attempt_id / artifact)
        return [path for path in candidates if path.is_file()]

    def _finalize(self, state: BuildState) -> BuildState:
        if not state.schema_confirmed:
            raise StateConflictError("Schema must be confirmed before Workspace finalization")
        paths = self.layout.session(state.session_id)
        schema_review = state.schema_review or {}
        evidence = state.evidence or {}
        draft_paths = self._accepted_draft_paths(state)
        readme_path = canonical_artifact_path(paths, str(Stage.DOCUMENT))
        if not readme_path.is_file():
            raise MissingStateError("Accepted Workspace README is missing", path=str(readme_path))
        readme = readme_path.read_text(encoding="utf-8")
        try:
            result = finalize_workspace(
                paths=paths,
                schema_source=str(schema_review.get("schema_source") or ""),
                draft_paths=draft_paths,
                sources=list(evidence.get("sources") or []),
                schema_version=int(state.stage_attempts.get(Stage.SCHEMA_BUILD, 1)),
                data_version=max(
                    int(state.stage_attempts.get(Stage.EXTRACT, 1)),
                    int(state.stage_attempts.get(Stage.STRUCTURED_EXTRACT, 1)),
                ),
                readme=readme,
            )
        except BuilderError as exc:
            failed = self.states.update(
                state.session_id,
                state.version,
                lambda current, error=exc: self._set_failure(current, error),
            )
            self.events.append(
                state.session_id,
                kind="stage_state",
                status="failed",
                stage=Stage.FINALIZE,
                report=str(exc),
                public_data={"error": exc.detail.to_dict()},
            )
            return failed

        ready = self.states.update(state.session_id, state.version, self._set_ready)
        for stage, report in (
            (Stage.FINALIZE, "The executable knowledge Workspace passed final validation."),
            (Stage.READY, "Workspace ready."),
        ):
            self.events.append(
                state.session_id,
                kind="stage_state",
                status="completed",
                stage=stage,
                report=report,
                public_data=result,
            )
        return ready

    def _fail_revision_limit(self, state: BuildState) -> BuildState:
        error = ContractError(
            "Schema revision limit reached before a passing judgement",
            limit=self.schema_revision_limit,
        )
        failed = self.states.update(state.session_id, state.version, lambda current: self._set_failure(current, error))
        self.events.append(
            state.session_id,
            kind="stage_state",
            status="failed",
            stage=Stage.SCHEMA_BUILD,
            report=str(error),
            public_data={"error": error.detail.to_dict()},
        )
        return failed

    def _fail_preflight(
        self,
        state: BuildState,
        stage: Stage,
        error: BuilderError,
        turn_id: str,
    ) -> BuildState:
        failed = self.states.update(
            state.session_id,
            state.version,
            lambda current: self._set_failure(current, error),
        )
        self.events.append(
            state.session_id,
            kind="stage_state",
            status="failed",
            stage=stage,
            turn_id=turn_id,
            report=str(error),
            public_data={"error": error.detail.to_dict()},
        )
        return failed

    @staticmethod
    def _set_failure(current: BuildState, error: BuilderError) -> BuildState:
        current.status = "failed"
        current.failure = error.detail.to_dict()
        current.active_attempt_id = ""
        return current

    @staticmethod
    def _set_ready(current: BuildState) -> BuildState:
        if current.stage != Stage.FINALIZE:
            raise StateConflictError("Builder stage changed before final Workspace commit", stage=current.stage)
        current.stage = Stage.READY
        current.status = "workspace_ready"
        current.failure = None
        current.invalidated_from = ""
        current.pending_revision = ""
        current.active_follow_up_request = ""
        current.retry_reason = ""
        current.pending_evidence_step_indexes = []
        current.extension_baseline_steps = []
        current.replace_instances = False
        return current
