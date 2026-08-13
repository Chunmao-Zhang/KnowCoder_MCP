from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from knowcoder_workspace_builder.runtime.invocation_context import write_invocation_context
from knowcoder_workspace_builder.runtime.session_context import harness_session_environment
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.tools.stage_artifacts import save_problem_review
from knowcoder_workspace_builder.validation.artifact_validators import validate_current_artifact
from knowcoder_workspace_builder.validation.file_validation import load_repair_prompt


def _environment(values: dict[str, str]):
    class Environment:
        def __enter__(self):
            self.previous = {name: os.environ.get(name) for name in values}
            os.environ.update(values)

        def __exit__(self, *_args):
            for name, value in self.previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    return Environment()


def test_repair_prompts_are_case_based() -> None:
    for stage in (
        "problem",
        "evidence",
        "schema_build",
        "schema_judge",
        "extract",
        "structured_extract",
        "document",
    ):
        generic = load_repair_prompt(stage, errors=["generic failure"])
        assert "Matched repair case:" in generic
        assert len(generic) > 40

    extract_hint = load_repair_prompt("extract", errors=["Entity attributes must be an object"])
    assert "attributes" in extract_hint
    assert "JSON-compatible object" in extract_hint


def test_chat_completion_parser_is_not_available() -> None:
    assert importlib.util.find_spec("knowcoder_workspace_builder.runtime.stage_completion") is None


def test_stage_completion_requires_the_fixed_artifact(runtime_project: Path) -> None:
    layout = ProjectLayout(runtime_project)
    paths = layout.session("session-fixed-artifact-only", create=True)
    attempt = AttemptStore(layout).start(paths.session_id, "problem", 1)
    attempt_id = str(attempt["attempt_id"])
    stage_input = {
        "question": "Compare A and B.",
        "upload_paths": [],
        "current_date": "2026-07-23",
        "workspace_context": {"workspace_catalog": []},
    }
    write_invocation_context(paths, attempt_id, "problem", stage_input)

    with harness_session_environment(paths, attempt_id) as environment:
        with _environment(dict(environment)):
            missing = validate_current_artifact("problem", stage_input=stage_input, validation_round=1)
            assert missing.ok is False
            assert "artifact does not exist" in " ".join(missing.errors)

            saved = json.loads(
                save_problem_review.invoke(
                    {
                        "workspace_action": "new",
                        "scope": {"objects": ["A", "B"]},
                        "steps": ["Collect A", "Collect B"],
                        "missing_information": [],
                    }
                )
            )
            assert saved["ok"] is True
            validated = validate_current_artifact("problem", stage_input=stage_input, validation_round=2)

    assert validated.ok is True
    assert validated.payload["question"] == stage_input["question"]
    assert validated.payload["base_workspace_id"] == ""
    assert (paths.attempts / attempt_id / "normalization_log.json").is_file()
