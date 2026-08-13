from __future__ import annotations

import json
from pathlib import Path

from knowcoder_workspace_builder.contracts.agent import StageResult
from knowcoder_workspace_builder.service.builder import BuilderService


class _BoomEvidenceRunner:
    """Mimics an evidence specialist that fails before persisting a candidate."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, *, stage: str, stage_input: dict, paths, attempt_id: str, on_event=None) -> StageResult:
        self.calls.append(stage)
        if stage == "problem":
            handoff = {
                "workspace_action": "new",
                "base_workspace_id": "",
                "question": stage_input["question"],
                "scope": {"objects": ["Argentina"]},
                "steps": [
                    "Collect Argentina World Cup history",
                    "Collect USA host-effect data",
                ],
                "missing_information": [],
            }
            artifact = paths.attempts / attempt_id / "problem_review.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(handoff), encoding="utf-8")
            return StageResult(
                ok=True,
                stage=stage,
                status="completed",
                report="problem ok",
                handoff=handoff,
                artifacts={"problem_review": paths.relative_to_project(artifact)},
            )
        if stage == "evidence":
            # No candidate files written on purpose.
            return StageResult(
                ok=False,
                stage=stage,
                status="failed",
                report="evidence_collector completion could not be accepted: synthetic failure",
                handoff={},
                errors=("evidence_collector completion could not be accepted: synthetic failure",),
            )
        # should not reach later stages in this test
        return StageResult(
            ok=False,
            stage=stage,
            status="failed",
            report=f"unexpected stage {stage}",
            handoff={},
            errors=(f"unexpected stage {stage}",),
        )

    def cancel(self, attempt_id: str) -> bool:
        return True


def test_frontend_confirm_problem_continue_does_not_crash_on_missing_candidate(runtime_project: Path) -> None:
    service = BuilderService(runtime_project, agent_runner=_BoomEvidenceRunner(), recover_interrupted=False)
    gate = service.start_workspace_build(
        question="Assess Argentina World Cup probability with USA host effect.",
        workspace_id="session-frontend-missing-candidate",
        turn_id="t1",
    )
    assert gate["status"] == "needs_problem_confirmation"

    after_confirm = service.resume_workspace_build(
        workspace_id=gate["session_id"],
        confirmation_type="problem",
        user_confirmed=True,
        expected_version=gate["version"],
        turn_id="t2",
    )
    assert after_confirm["status"] == "running"
    assert after_confirm["next_action"] == "resume_builder"

    continued = service.resume_workspace_build(
        workspace_id=gate["session_id"],
        expected_version=after_confirm["version"],
        turn_id="t2",
    )
    # Must fail as a controlled stage failure, not an infra crash string alone without stage context.
    assert continued["status"] == "failed"
    failure = continued.get("failure") or {}
    blob = json.dumps({"failure": failure, "errors": continued.get("errors")}, ensure_ascii=False)
    # Controlled stage failure is fine; the frontend-path infra crash must not appear.
    assert "Saved stage candidate is missing" not in blob
    assert continued.get("stage") == "evidence" or (failure.get("stage") in {None, "evidence"})
