from __future__ import annotations

import pytest

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.contracts.builder import BuildResponse
from knowcoder_workspace_builder.contracts.errors import ContractError
from knowcoder_workspace_builder.contracts.events import InvocationEvent


def test_stage_result_separates_public_report_and_private_handoff() -> None:
    result = StageResult(
        ok=True,
        stage="problem",
        status="completed",
        report="Clarified the requested comparison and its time range.",
        handoff={"question": "Compare the records."},
        artifacts={"problem_review": ".knowcoder_workspace/sessions/example/intermediate/problem.json"},
    )

    assert "handoff" in result.to_dict()
    assert "handoff" not in result.to_dict(include_private=False)


def test_completed_result_requires_a_report() -> None:
    with pytest.raises(ContractError, match="non-empty completion report"):
        StageResult(ok=True, stage="problem", status="completed", report="", handoff={})


def test_builder_response_rejects_unknown_next_action() -> None:
    with pytest.raises(ContractError, match="invalid next action"):
        BuildResponse(
            ok=True,
            session_id="session-1234",
            status="running",
            stage="clarify",
            version=1,
            next_action="guess",
            message="Running.",
        )


def test_private_event_never_has_a_public_representation() -> None:
    event = InvocationEvent(
        session_id="session-1234",
        sequence=1,
        kind="handoff",
        status="completed",
        visibility="private",
        private_data={"internal": "next-stage context"},
    )

    assert event.to_public_dict() is None
