from __future__ import annotations

from pathlib import Path

import pytest

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.review.service import ReviewService
from knowcoder_workspace_builder.service.builder import BuilderService
from knowcoder_workspace_builder.storage.schema import parse_schema
from knowcoder_workspace_builder.storage.sessions import BuildStateStore
from knowcoder_workspace_builder.storage.transaction import AtomicWriter
from knowcoder_workspace_builder.workflow.stages import Stage


SCHEMA_SOURCE = """from typing import List


class Entity:
    name: str


class Person(Entity):
    \"\"\"A source person.\"\"\"
    _id: str
    name: str
    employers: List[\"Company\"]
    \"\"\"Links the person to their employers.\"\"\"


class Company(Entity):
    \"\"\"A source company.\"\"\"
    _id: str
    name: str
"""

SCHEMA_SOURCE_WITHOUT_DESCRIPTIONS = """class Entity:
    name: str


class Person(Entity):
    _id: str
    name: str
    employer: "Company"


class Company(Entity):
    _id: str
    name: str
"""


class PassingJudgeRunner:
    def run(self, *, stage: str, stage_input: dict[str, object], paths, attempt_id: str, on_event=None) -> StageResult:
        assert stage == "schema_judge"
        artifact = paths.attempts / attempt_id / "schema_judgement.json"
        AtomicWriter(paths).json(artifact, {})
        return StageResult(
            ok=True,
            stage=stage,
            status="completed",
            report="The edited schema passed revalidation.",
            handoff={
                "decision": "pass",
                "missing_requirements": [],
                "coverage": [
                    {
                        "step": stage_input["steps"][0],
                        "status": "supported",
                        "support": ["Person.employers -> Company"],
                        "missing": [],
                    }
                ],
            },
            artifacts={"schema_judgement": paths.relative_to_project(artifact)},
        )

    def cancel(self, attempt_id: str) -> bool:
        del attempt_id
        return False


def test_unchanged_schema_form_preserves_source_without_revalidation(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, agent_runner=PassingJudgeRunner(), recover_interrupted=False)
    states = BuildStateStore(service.layout)
    initial = states.create("List people and employers.", [], session_id="session-review-unchanged-1234")
    gate = states.update(initial.session_id, initial.version, _schema_gate)
    review_service = ReviewService(service.layout)

    saved = review_service.save_schema_form(gate.session_id, ReviewService.form_from_schema(parse_schema(SCHEMA_SOURCE)))

    assert saved["requires_revalidation"] is False
    current = states.load(gate.session_id)
    assert current.schema_review["schema_source"] == SCHEMA_SOURCE


def test_schema_without_descriptions_is_rejected() -> None:
    with pytest.raises(ContractError, match="non-empty"):
        parse_schema(SCHEMA_SOURCE_WITHOUT_DESCRIPTIONS)


def test_direct_schema_edit_requires_judgement_and_a_second_confirmation(runtime_project: Path) -> None:
    service = BuilderService(
        runtime_project,
        agent_runner=PassingJudgeRunner(),
        recover_interrupted=False,
    )
    states = BuildStateStore(service.layout)
    initial = states.create("List people and employers.", [], session_id="session-review-1234")
    gate = states.update(initial.session_id, initial.version, _schema_gate)
    review_service = ReviewService(service.layout)
    form = ReviewService.form_from_schema(parse_schema(SCHEMA_SOURCE))
    person = next(item for item in form if item.get("entity_type") == "Person")
    person["attributes"].append(
        {
            "name": "role",
            "attribute": "role",
            "type": "str",
            "attribute_data_type": "str",
            "optional": True,
        }
    )

    edited = review_service.save_schema_form(gate.session_id, form)
    assert edited["requires_revalidation"] is True

    current_review = review_service.get(gate.session_id, "schema")
    relation = current_review["relations"][0]
    assert relation["head"] == "Person"
    assert relation["tail"] == "Company"
    assert relation["many"] is True
    assert relation["optional"] is False
    assert relation["directed"] is True

    accepted_edit = service.resume_workspace_build(
        workspace_id=gate.session_id,
        confirmation_type="schema",
        user_confirmed=True,
        expected_version=edited["version"],
    )
    assert accepted_edit["status"] == "running"
    assert accepted_edit["stage"] == "schema_judge"

    rejudged = service.resume_workspace_build(
        workspace_id=gate.session_id,
        expected_version=accepted_edit["version"],
    )
    assert rejudged["status"] == "needs_schema_confirmation"
    assert rejudged["review"]["requires_revalidation"] is False

    confirmed = service.resume_workspace_build(
        workspace_id=gate.session_id,
        confirmation_type="schema",
        user_confirmed=True,
        expected_version=rejudged["version"],
    )
    assert confirmed["status"] == "running"
    assert confirmed["stage"] == "extract"


def test_schema_revision_instruction_is_not_silently_truncated(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, agent_runner=PassingJudgeRunner(), recover_interrupted=False)
    states = BuildStateStore(service.layout)
    initial = states.create("List people and employers.", [], session_id="session-long-schema-revision-1234")
    gate = states.update(initial.session_id, initial.version, _schema_gate)
    instruction = "Preserve this complete revision requirement. " * 30

    def mark_revision(current):
        current.pending_revision = instruction
        current.stage = Stage.SCHEMA_BUILD
        current.status = "running"
        return current

    revised = states.update(gate.session_id, gate.version, mark_revision)
    stage_input = service.coordinator._stage_input(revised, Stage.SCHEMA_BUILD)

    assert len(instruction) > 400
    assert stage_input["workspace_context"]["user_instruction"] == instruction.strip()


def _schema_gate(state):
    return _schema_gate_with_source(state, SCHEMA_SOURCE)


def _schema_gate_with_source(state, source: str):
    state.problem = {
        "question": state.question,
        "scope": {},
        "steps": ["Identify every person and employer relationship."],
        "missing_information": [],
    }
    state.problem_confirmed = True
    state.evidence = {"coverage": [], "sources": [], "unresolved_gaps": [], "blocking_gaps": []}
    state.schema_review = {
        "schema_source": source,
        "schema_outline": {},
        "judgement": {"decision": "pass", "missing_requirements": [], "coverage": [True]},
    }
    state.stage = Stage.SCHEMA_JUDGE
    state.status = "needs_schema_confirmation"
    return state
