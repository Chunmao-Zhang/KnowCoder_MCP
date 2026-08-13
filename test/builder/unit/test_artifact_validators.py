from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowcoder_workspace_builder.contracts.errors import StorageBoundaryError
from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.session_context import harness_session_environment
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.stage_artifacts import write_artifact
from knowcoder_workspace_builder.storage.transaction import read_json
from knowcoder_workspace_builder.validation.artifact_validators import (
    ArtifactValidator,
    get_artifact_validator,
    validate_current_artifact,
)
from knowcoder_workspace_builder.validation.file_validation import MAX_VALIDATION_ROUNDS
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS


class _Environment:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.previous: dict[str, str | None] = {}

    def __enter__(self):
        self.previous = {name: os.environ.get(name) for name in self.values}
        os.environ.update(self.values)

    def __exit__(self, *_args):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_every_stage_uses_the_shared_artifact_validator_base() -> None:
    for stage in STAGE_PROTOCOLS:
        validator = get_artifact_validator(stage)
        assert isinstance(validator, ArtifactValidator)
        assert validator.max_attempts == MAX_VALIDATION_ROUNDS == 2


def test_problem_validation_repairs_the_same_fixed_candidate(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-artifact-validator-problem", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "problem", 1)
    attempt_id = str(attempt["attempt_id"])
    stage_input = {
        "question": "Compare A and B.",
        "upload_paths": [],
        "current_date": "2026-07-22",
        "workspace_context": {"mode": "new"},
    }
    write_invocation_context(paths, attempt_id, "problem", stage_input)
    write_artifact(
        paths,
        attempt_id,
        "problem_review",
        {
            "workspace_action": "new",
            "base_workspace_id": "",
            "question": "Compare A and B.",
            "scope": {"objects": ["A", "B"]},
            "steps": [],
            "missing_information": [],
        },
    )

    with harness_session_environment(paths, attempt_id) as environment, _Environment(dict(environment)):
        failed = validate_current_artifact("problem", stage_input=stage_input, validation_round=1)
        assert failed.ok is False
        assert failed.retryable is True
        assert failed.outcome.context["reason"] == "model_output_invalid"
        candidate_path = failed.feedback()["candidate_path"]

        write_artifact(
            paths,
            attempt_id,
            "problem_review",
            {
                "workspace_action": "new",
                "base_workspace_id": "",
                "question": "Compare A and B.",
                "scope": {"objects": ["A", "B"]},
                "steps": ["Collect comparable records for A and B."],
                "missing_information": [],
            },
        )
        passed = validate_current_artifact("problem", stage_input=stage_input, validation_round=2)
        result = passed.to_stage_result(stage_input=stage_input)
        passed_candidate_path = passed.feedback()["candidate_path"]

    assert passed.ok is True
    assert result.handoff["steps"] == ["Collect comparable records for A and B."]
    assert passed_candidate_path == candidate_path
    log = read_json(paths.attempts / attempt_id / "validation_log.json")
    assert [item["ok"] for item in log["rounds"]] == [False, True]


def test_storage_failure_is_not_sent_to_the_model_as_artifact_repair(
    runtime_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-artifact-validator-storage", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "problem", 1)
    attempt_id = str(attempt["attempt_id"])
    stage_input = {
        "question": "Compare A and B.",
        "upload_paths": [],
        "current_date": "2026-07-22",
        "workspace_context": {"mode": "new"},
    }
    write_invocation_context(paths, attempt_id, "problem", stage_input)
    candidate = write_artifact(paths, attempt_id, "problem_review", {"placeholder": True})
    validator = get_artifact_validator("problem")

    def fail_read(_path: Path) -> object:
        raise StorageBoundaryError("candidate storage is unavailable")

    monkeypatch.setattr(validator, "_read", fail_read)
    result = validator.validate_path(candidate, stage_input=stage_input)

    assert result.ok is False
    assert result.retryable is False
    assert result.outcome.context["reason"] == "system_error"


def test_evidence_validation_requires_registered_upload_in_sources_and_coverage(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-artifact-validator-evidence", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "evidence", 1)
    attempt_id = str(attempt["attempt_id"])
    step = "Collect the supplied records."
    stage_input = {
        "question": "Review the supplied records.",
        "steps": [step],
        "upload_paths": ["/.knowcoder_workspace/intermediate/sources/user_uploads/data.xlsx"],
        "research_dir": "/.knowcoder_workspace/intermediate",
        "workspace_context": {"required_source_ids": ["upload-data"]},
    }
    write_invocation_context(paths, attempt_id, "evidence", stage_input)
    candidate = write_artifact(
        paths,
        attempt_id,
        "evidence_manifest",
        {
            "coverage": [
                {
                    "step": step,
                    "requirements": ["supplied records"],
                    "source_ids": ["source-web"],
                    "status": "covered",
                }
            ],
            "sources": [{"source_id": "source-web"}],
            "unresolved_gaps": [],
            "blocking_gaps": [],
        },
    )

    validator = get_artifact_validator("evidence")
    failed = validator.validate_path(candidate, stage_input=stage_input)
    assert failed.ok is False
    assert "upload-data" in failed.errors[0]

    write_artifact(
        paths,
        attempt_id,
        "evidence_manifest",
        {
            "coverage": [
                {
                    "step": step,
                    "requirements": ["supplied records"],
                    "source_ids": ["source-web", "upload-data"],
                    "status": "covered",
                }
            ],
            "sources": [{"source_id": "source-web"}, {"source_id": "upload-data"}],
            "unresolved_gaps": [],
            "blocking_gaps": [],
        },
    )
    assert validator.validate_path(candidate, stage_input=stage_input).ok is True


def test_structured_extractor_counts_are_derived_from_the_saved_file(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-artifact-validator-structured", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "structured_extract", 1)
    attempt_id = str(attempt["attempt_id"])
    stage_input = {
        "schema_outline": {"entities": []},
        "sources": [{"source_id": "source-1"}],
        "draft_path": "/.knowcoder_workspace/intermediate/attempts/draft/structured_draft.json",
        "work_dir": "/.knowcoder_workspace/intermediate/attempts/draft/work",
        "batch_path": "/.knowcoder_workspace/intermediate/attempts/draft/structured_batches.json",
        "workspace_context": {},
    }
    write_invocation_context(paths, attempt_id, "structured_extract", stage_input)
    write_artifact(
        paths,
        attempt_id,
        "structured_draft",
        {
            "format_version": 1,
            "processed_source_ids": ["source-1"],
            "entities": [],
            "relations": [],
        },
    )

    with harness_session_environment(paths, attempt_id) as environment, _Environment(dict(environment)):
        validation = validate_current_artifact(
            "structured_extract",
            stage_input=stage_input,
            validation_round=1,
        )
        result = validation.to_stage_result(stage_input=stage_input)

    assert validation.ok is True
    assert result.handoff == {
        "processed_source_ids": ["source-1"],
        "entity_count": 0,
        "relation_count": 0,
    }


def test_validation_log_write_failure_is_not_silenced(
    runtime_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-artifact-validator-log-failure", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "problem", 1)
    attempt_id = str(attempt["attempt_id"])
    stage_input = {
        "question": "Compare A and B.",
        "upload_paths": [],
        "current_date": "2026-07-22",
        "workspace_context": {"mode": "new"},
    }
    write_invocation_context(paths, attempt_id, "problem", stage_input)
    write_artifact(
        paths,
        attempt_id,
        "problem_review",
        {
            "workspace_action": "new",
            "base_workspace_id": "",
            "question": "Compare A and B.",
            "scope": {"objects": ["A", "B"]},
            "steps": ["Collect comparable records for A and B."],
            "missing_information": [],
        },
    )

    def fail_to_record(*args: object, **kwargs: object) -> None:
        raise OSError("validation log is unavailable")

    monkeypatch.setattr(
        "knowcoder_workspace_builder.validation.artifact_validators.record_validation_round",
        fail_to_record,
    )
    with harness_session_environment(paths, attempt_id) as environment, _Environment(dict(environment)):
        with pytest.raises(OSError, match="validation log is unavailable"):
            validate_current_artifact("problem", stage_input=stage_input, validation_round=1)
