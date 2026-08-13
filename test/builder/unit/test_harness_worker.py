from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from knowcoder_workspace_builder.contracts.errors import ContractError, ExternalServiceError
from knowcoder_workspace_builder.runtime.harness_worker import (
    DEFAULT_OUTPUT_TOKENS,
    _configured_output_tokens,
    _configure_validation_repair,
    _coordinator_for_stage,
    _emit_validation_repair,
    _raise_required_tool_failure,
    _run_coordinator,
    _schema_unit_input,
    _specialist_for_stage,
    _validation_repair_message,
)
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_delegation_payload,
    bind_delegation_payload,
)


class _Registry:
    def __init__(self) -> None:
        self.requested: list[str] = []
        self.coordinator = SimpleNamespace(id="workspace_builder", subagents=[])

    def get(self, agent_id: str) -> SimpleNamespace:
        self.requested.append(agent_id)
        return SimpleNamespace(id=agent_id)

    def get_default(self) -> SimpleNamespace:
        return self.coordinator


@dataclass(frozen=True)
class _Model:
    max_tokens: int = 8_192


def _evidence_input() -> dict:
    """Minimal evidence-stage input satisfying the current stage-input contract."""
    return {
        "question": "Collect current data.",
        "steps": ["Collect current data."],
        "upload_paths": [],
        "research_dir": "research",
        "workspace_context": {},
    }


def test_output_token_configuration_has_an_explicit_16384_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("TEST_OUTPUT_TOKENS", "16385")
    with pytest.raises(ValueError, match="between 1 and 16384"):
        _configured_output_tokens("TEST_OUTPUT_TOKENS", DEFAULT_OUTPUT_TOKENS, DEFAULT_OUTPUT_TOKENS)

    monkeypatch.setenv("TEST_OUTPUT_TOKENS", "8192")
    assert _configured_output_tokens("TEST_OUTPUT_TOKENS", DEFAULT_OUTPUT_TOKENS, DEFAULT_OUTPUT_TOKENS) == 8192


def test_each_stage_is_dispatched_by_the_default_coordinator() -> None:
    registry = _Registry()

    selected = {
        stage: _coordinator_for_stage(
            stage,
            registry,
            _specialist_for_stage(stage, registry),
        ).subagents
        for stage in STAGE_PROTOCOLS
    }

    assert selected == {stage: [protocol.agent] for stage, protocol in STAGE_PROTOCOLS.items()}
    assert registry.requested == [protocol.agent for protocol in STAGE_PROTOCOLS.values()]


def test_unknown_stage_fails_before_model_execution() -> None:
    with pytest.raises(ValueError, match="Unknown Builder stage"):
        _specialist_for_stage("unknown", _Registry())


def test_validation_feedback_is_bound_into_the_subagent_payload() -> None:
    payload = {
        "stage": "evidence",
        "input": _evidence_input(),
        "validation_feedback": {"ok": False, "errors": ["Repair the saved manifest."]},
        "repair_mode": "edit_saved_candidate",
    }

    with bind_delegation_payload(payload):
        delegated = active_delegation_payload()

    assert delegated["validation_feedback"] == payload["validation_feedback"]
    assert delegated["repair_mode"] == "edit_saved_candidate"


def test_required_tool_failure_preserves_the_last_validation_error() -> None:
    result = {
        "messages": [
            ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "error_type": "invalid_schema",
                        "error": "Entity Company requires a non-empty description.",
                    }
                ),
                name="save_schema",
                tool_call_id="save-schema-1",
            )
        ]
    }

    with pytest.raises(ContractError, match="Company requires a non-empty description"):
        _raise_required_tool_failure(
            result,
            "save_schema",
            "Schema save failed.",
            stage="schema_build",
        )


def test_required_tool_failure_preserves_external_service_type() -> None:
    result = {
        "messages": [
            ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "error_type": "ExternalServiceError",
                        "error": "Extraction API key is missing.",
                    }
                ),
                name="extract_unstructured_chunks",
                tool_call_id="extract-1",
            )
        ]
    }

    with pytest.raises(ExternalServiceError, match="API key is missing"):
        _raise_required_tool_failure(
            result,
            "extract_unstructured_chunks",
            "Extraction failed.",
            stage="extract",
        )


def test_schema_unit_input_contains_one_batch_and_current_outline(monkeypatch) -> None:
    monkeypatch.setattr(
        "knowcoder_workspace_builder.runtime.harness_worker._schema_outline_for_attempt",
        lambda _stage_input: {
            "entities": [
                {
                    "name": "Existing",
                    "id_type": "str",
                    "description": "Long text excluded from the compact projection.",
                    "attributes": [{"name": "value", "type": "str", "optional": True}],
                }
            ],
            "relations": [],
        },
    )
    stage_input = {
        "question": "Compare records.",
        "steps": ["Collect companies.", "Compare revenue."],
        "data_manifest": {
            "coverage": [
                {"step_index": 1, "requirements": ["Company names"]},
                {"step_index": 2, "requirements": ["Revenue values"]},
            ],
            "unresolved_gaps": [],
        },
        "workspace_context": {"current_schema": "large accepted schema", "mode": "new"},
    }

    unit = _schema_unit_input(
        stage_input,
        steps=[stage_input["steps"][1]],
        batch_index=2,
        batch_total=2,
        coverage_step_indexes=[2],
    )

    assert unit["steps"] == ["Compare revenue."]
    assert unit["data_manifest"]["coverage"] == [{"step_index": 2, "requirements": ["Revenue values"]}]
    assert unit["workspace_context"]["current_schema_outline"]["entities"][0]["name"] == "Existing"
    assert "description" not in unit["workspace_context"]["current_schema_outline"]["entities"][0]
    assert "current_schema" not in unit["workspace_context"]


def test_schema_unit_input_uses_original_coverage_index_for_filtered_steps(monkeypatch) -> None:
    monkeypatch.setattr(
        "knowcoder_workspace_builder.runtime.harness_worker._schema_outline_for_attempt",
        lambda _stage_input: {"entities": [], "relations": []},
    )
    stage_input = {
        "question": "Extend a schema.",
        "steps": ["Collect the newly added requirement."],
        "data_manifest": {
            "coverage": [
                {"step_index": 1, "requirements": ["Baseline"]},
                {"step_index": 11, "requirements": ["New requirement"]},
            ]
        },
        "workspace_context": {},
    }

    unit = _schema_unit_input(
        stage_input,
        steps=[stage_input["steps"][0]],
        batch_index=1,
        batch_total=1,
        coverage_step_indexes=[11],
    )

    assert unit["data_manifest"]["coverage"] == [{"step_index": 11, "requirements": ["New requirement"]}]
    assert unit["workspace_context"]["source_step_indexes"] == [11]


def test_evidence_length_limit_retries_once_with_compact_feedback(monkeypatch, tmp_path) -> None:
    class LengthFinishReasonError(RuntimeError):
        pass

    calls: list[str] = []
    thread_ids: list[str] = []

    def stream_agent(*_args, **kwargs):
        calls.append(kwargs["message"])
        thread_ids.append(kwargs["thread_id"])
        if len(calls) == 1:
            raise LengthFinishReasonError("output limit")
        return {"messages": []}

    monkeypatch.setattr("knowcoder_workspace_builder.runtime.harness_worker.stream_agent", stream_agent)
    monkeypatch.setenv("HARNESS_RUN_DIR", str(tmp_path))
    coordinator = SimpleNamespace(model=_Model())
    result, _emitter = _run_coordinator(
        stage="evidence",
        coordinator=coordinator,
        subagent_id="evidence_collector",
        registry=SimpleNamespace(),
        config=SimpleNamespace(),
        message=json.dumps({"stage": "evidence", "input": _evidence_input()}),
    )

    assert result == {"messages": []}
    assert len(calls) == 2
    assert thread_ids[0] == thread_ids[1]
    assert 0 < coordinator.model.max_tokens <= 16_384
    retry = json.loads(calls[1])
    feedback = retry["validation_feedback"]["errors"][0]
    assert "Reuse successful tool results and registered sources" in feedback
    assert retry["input"] == _evidence_input()


def test_extract_validation_repair_message_keeps_validated_stage_context() -> None:
    payload = json.loads(
        _validation_repair_message(
            "extract",
            {"sources": [{"source_id": "source-a"}]},
            {"errors": ["The saved draft is invalid."]},
        )
    )

    assert payload["stage"] == "extract"
    assert payload["input"]["sources"][0]["source_id"] == "source-a"
    assert payload["validation_feedback"]["errors"] == ["The saved draft is invalid."]
    assert payload["repair_mode"] == "edit_saved_candidate"


def test_validation_repair_emits_visible_round_activity(monkeypatch) -> None:
    events: list[dict] = []
    monkeypatch.setattr("knowcoder_workspace_builder.runtime.harness_worker.emit_worker_event", events.append)

    _emit_validation_repair("problem", 2, "running")
    _emit_validation_repair("problem", 2, "done")

    assert [event["message"]["status"] for event in events] == ["running", "done"]
    assert all(event["message"]["validation_round"] == 2 for event in events)
    assert "2/2" in events[0]["message"]["content"]


def test_evidence_validation_repair_keeps_research_tools_available() -> None:
    specialist = SimpleNamespace(
        id="evidence_collector",
        tools=SimpleNamespace(
            allow=["source_reader", "web_search", "web_search_batch", "fetch_web_pages", "save_evidence_manifest"]
        ),
    )

    _configure_validation_repair("evidence", specialist)

    assert specialist.tools.allow == [
        "web_search",
        "web_search_batch",
        "fetch_web_pages",
        "save_evidence_manifest",
    ]


def test_missing_evidence_artifact_repair_only_exposes_persistence() -> None:
    specialist = SimpleNamespace(
        id="evidence_collector",
        tools=SimpleNamespace(
            allow=["source_reader", "web_search", "web_search_batch", "fetch_web_pages", "save_evidence_manifest"]
        ),
    )

    _configure_validation_repair("evidence", specialist, reason="missing_artifact")

    assert specialist.tools.allow == ["save_evidence_manifest"]


def test_validation_repair_uses_fresh_thread_and_restricts_tools(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, list[str], str]] = []

    def stream_agent(_agent, *_args, **kwargs):
        calls.append((kwargs["thread_id"], list(specialist.tools.allow), kwargs["message"]))
        return {"messages": []}

    monkeypatch.setattr("knowcoder_workspace_builder.runtime.harness_worker.stream_agent", stream_agent)
    monkeypatch.setenv("HARNESS_RUN_DIR", str(tmp_path))
    specialist = SimpleNamespace(
        id="evidence_collector",
        model=_Model(),
        tools=SimpleNamespace(
            allow=["source_reader", "web_search", "web_search_batch", "fetch_web_pages", "save_evidence_manifest"]
        ),
    )
    common = {
        "stage": "evidence",
        "coordinator": SimpleNamespace(model=_Model()),
        "subagent_id": specialist.id,
        "registry": SimpleNamespace(),
        "config": SimpleNamespace(),
    }

    _run_coordinator(**common, message=json.dumps({"stage": "evidence", "input": _evidence_input()}))
    _configure_validation_repair("evidence", specialist)
    _run_coordinator(
        **common,
        message=json.dumps({"stage": "evidence", "input": _evidence_input(), "validation_feedback": {}}),
        thread_suffix="validation-2",
    )

    assert calls[0][0] != calls[1][0]
    assert calls[1][0].endswith(":validation-2")
    assert calls[0][1] == [
        "source_reader",
        "web_search",
        "web_search_batch",
        "fetch_web_pages",
        "save_evidence_manifest",
    ]
    assert calls[1][1] == [
        "web_search",
        "web_search_batch",
        "fetch_web_pages",
        "save_evidence_manifest",
    ]
