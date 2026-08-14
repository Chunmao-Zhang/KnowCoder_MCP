"""One-shot subprocess entrypoint for the protected Builder Harness."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import ToolMessage

from knowcoder_workspace_builder.agents.registry import (
    BUILDER_ROOT,
    load_harness_registry,
)
from knowcoder_workspace_builder.contracts.errors import (
    TRANSIENT_EXTERNAL_ERROR_TYPES,
    ContractError,
    ExternalServiceError,
)
from knowcoder_workspace_builder.harness.agents.agent_loop import stream_agent
from knowcoder_workspace_builder.runtime.agent_tool_call_middleware import (
    VALIDATION_ROUND_ENV,
)
from knowcoder_workspace_builder.runtime.invocation_context import (
    active_invocation_context,
    bind_delegation_payload,
)
from knowcoder_workspace_builder.runtime.live_events import (
    WorkerLiveEmitter,
    emit_worker_event,
)
from knowcoder_workspace_builder.runtime.session_context import active_session_paths
from knowcoder_workspace_builder.storage.tool_calls import ToolCallLedger
from knowcoder_workspace_builder.tools.workspace_readme_browser import (
    workspace_readme_browser,
)
from knowcoder_workspace_builder.validation.artifact_validators import (
    validate_current_artifact,
)
from knowcoder_workspace_builder.validation.file_validation import (
    MAX_VALIDATION_ROUNDS,
    STAGE_PERSISTENCE_TOOLS,
)
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS

SCHEMA_OUTPUT_TOKENS_ENV = "SCHEMA_BUILDER_MAX_TOKENS"
SCHEMA_RETRY_OUTPUT_TOKENS_ENV = "SCHEMA_BUILDER_RETRY_MAX_TOKENS"
EXTRACT_OUTPUT_TOKENS_ENV = "EXTRACTOR_MAX_TOKENS"
EXTRACT_RETRY_OUTPUT_TOKENS_ENV = "EXTRACTOR_RETRY_MAX_TOKENS"
STAGE_RETRY_OUTPUT_TOKENS_ENV = "SCHEMA_STAGE_RETRY_MAX_TOKENS"
DEFAULT_OUTPUT_TOKENS = 16_384
DEFAULT_STAGE_RETRY_OUTPUT_TOKENS = 16_384
DEFAULT_SCHEMA_OUTPUT_TOKENS = 4_096
DEFAULT_SCHEMA_RETRY_OUTPUT_TOKENS = 8_192
DEFAULT_EXTRACT_OUTPUT_TOKENS = 16_384
DEFAULT_EXTRACT_RETRY_OUTPUT_TOKENS = 16_384
LENGTH_RETRY_STAGES = frozenset(
    {"problem", "evidence", "schema_build", "schema_judge", "extract", "structured_extract", "document"}
)


def _configured_output_tokens(env_name: str, default: int, model_cap: int) -> int:
    configured = os.environ.get(env_name)
    if configured is None or not configured.strip():
        return min(default, model_cap)
    requested = int(configured)
    if requested < 1 or requested > DEFAULT_OUTPUT_TOKENS:
        raise ValueError(f"{env_name} must be between 1 and {DEFAULT_OUTPUT_TOKENS}")
    if requested > model_cap:
        raise ValueError(f"{env_name} exceeds the selected model output capacity of {model_cap}")
    return requested


SPECIALIST_RUNTIME = {
    "problem": {
        "name": "Problem Analyst",
        "tools": ("workspace_readme_browser", "source_reader", "save_problem_review"),
    },
    "evidence": {
        "name": "Data Collection Specialist",
        "tools": (
            "workspace_readme_browser",
            "source_reader",
            "web_search",
            "web_search_batch",
            "fetch_web_pages",
            "save_evidence_manifest",
        ),
    },
    "schema_build": {
        "name": "Schema Engineer",
        "tools": ("build_schema_candidates", "save_schema"),
    },
    "schema_judge": {
        "name": "Schema Quality Reviewer",
        "tools": ("workspace_readme_browser", "schema_validator", "save_schema_judgement"),
    },
    "extract": {
        "name": "Unstructured Data Extractor",
        "tools": ("extract_unstructured_chunks",),
    },
    "structured_extract": {
        "name": "Structured Data Extractor",
        "tools": (
            "workspace_readme_browser",
            "source_reader",
            "get_schema_outline",
            "write_file",
            "execute_code",
            "append_instances_batches_from_file",
        ),
    },
    "document": {
        "name": "Workspace Documenter",
        "tools": ("workspace_readme_browser", "save_workspace_readme"),
    },
}

VALIDATION_REPAIR_TOOLS = {
    "problem": ("save_problem_review",),
    "evidence": ("web_search", "web_search_batch", "fetch_web_pages", "save_evidence_manifest"),
    "schema_build": ("save_schema",),
    "schema_judge": ("save_schema_judgement",),
    "extract": ("extract_unstructured_chunks",),
    "structured_extract": ("write_file", "execute_code", "append_instances_batches_from_file"),
    "document": ("save_workspace_readme",),
}


def _emit_validation_repair(stage: str, round_index: int, status: str) -> None:
    if round_index < 2 or round_index > MAX_VALIDATION_ROUNDS:
        raise ValueError("Validation repair round is outside the configured range")
    if status not in {"running", "done", "failed"}:
        raise ValueError("Validation repair status is invalid")
    if status == "running":
        content = f"Validator requested file repair. Running round {round_index}/{MAX_VALIDATION_ROUNDS}."
    elif status == "done":
        content = f"Saved candidate passed validation in round {round_index}/{MAX_VALIDATION_ROUNDS}."
    else:
        content = f"Saved candidate failed validation in round {round_index}/{MAX_VALIDATION_ROUNDS}."
    emit_worker_event(
        {
            "type": "activity",
            "run_agent": "workspace_builder",
            "message": {
                "role": "event",
                "kind": "tool",
                "content": content,
                "tool": "artifact_validator",
                "tool_call_id": f"artifact-validation-repair:{stage}:{round_index}",
                "status": status,
                "stage": stage,
                "run_agent": "workspace_builder",
                "validation_round": round_index,
            },
        }
    )


def _emit_subagent_lifecycle(
    stage: str,
    status: str,
    *,
    unit_index: int = 0,
    unit_total: int = 0,
    workflow_started_at: str = "",
) -> None:
    if status not in {"running", "validating"}:
        raise ValueError("Subagent lifecycle status is invalid")
    if unit_index <= 0:
        context = active_invocation_context()
        workspace_context = context.input.get("workspace_context")
        detected_index = workspace_context.get("current_step_index") if isinstance(workspace_context, dict) else None
        unit_index = detected_index if isinstance(detected_index, int) else 0
    payload: dict[str, Any] = {
        "type": "stage",
        "stage": stage,
        "status": status,
        "agent": STAGE_PROTOCOLS[stage].agent,
        "run_agent": "workspace_builder",
        "subagent_lifecycle": True,
    }
    if status == "running":
        payload["started_at"] = workflow_started_at or datetime.now(UTC).isoformat()
    if workflow_started_at:
        payload["workflow_started_at"] = workflow_started_at
    if isinstance(unit_index, int) and not isinstance(unit_index, bool) and unit_index > 0:
        payload["extract_unit_index"] = unit_index
    emit_worker_event(payload)


def _specialist_for_stage(stage: str, registry: Any) -> Any:
    protocol = STAGE_PROTOCOLS.get(stage)
    if protocol is None:
        raise ValueError(f"Unknown Builder stage: {stage}")
    specialist = registry.get(protocol.agent)
    if specialist.id != protocol.agent:
        raise ValueError(f"Protected Harness is missing the declared Subagent for {stage}")
    return specialist


def _coordinator_for_stage(stage: str, registry: Any, specialist: Any) -> Any:
    coordinator = registry.get_default()
    if coordinator is None or coordinator.id != "workspace_builder":
        raise ValueError("Protected Harness is missing the workspace_builder Coordinator")
    if specialist.id != STAGE_PROTOCOLS[stage].agent:
        raise ValueError(f"Builder stage {stage} has a conflicting Subagent owner")
    coordinator.subagents = [specialist.id]
    return coordinator


def _configure_specialist(stage: str, specialist: Any, stage_input: dict[str, Any]) -> None:
    runtime = SPECIALIST_RUNTIME[stage]
    tools = getattr(specialist, "tools", None)
    if tools is None:
        raise ValueError(f"Builder Subagent {specialist.id} is missing its tool configuration")
    declared = set(tools.allow)
    required = set(runtime["tools"])
    missing = sorted(required - declared)
    if missing:
        raise ValueError(f"Builder Subagent {specialist.id} is missing required tools: {', '.join(missing)}")
    allowed_tools = [name for name in runtime["tools"] if name != "workspace_readme_browser"]
    if stage in {"problem", "evidence"} and not stage_input.get("upload_paths"):
        allowed_tools = [name for name in allowed_tools if name != "source_reader"]
    tools.allow = allowed_tools
    specialist.name = str(runtime["name"])
    specialist.description = f"Runs the {runtime['name']} stage."
    # Stage defaults may request large outputs, but never exceed the selected
    # model capacity. Friday pro-baidu rejects oversized max_tokens (400).
    model_cap = min(DEFAULT_OUTPUT_TOKENS, int(specialist.model.max_tokens or DEFAULT_OUTPUT_TOKENS))
    output_tokens = model_cap
    if stage == "schema_build":
        output_tokens = _configured_output_tokens(
            SCHEMA_OUTPUT_TOKENS_ENV,
            DEFAULT_SCHEMA_OUTPUT_TOKENS,
            model_cap,
        )
    elif stage in {"extract", "structured_extract"}:
        output_tokens = _configured_output_tokens(
            EXTRACT_OUTPUT_TOKENS_ENV,
            DEFAULT_EXTRACT_OUTPUT_TOKENS,
            model_cap,
        )
    specialist.model = replace(
        specialist.model,
        max_tokens=output_tokens,
        response_format=None,
    )


def _load_workspace_snapshot() -> dict[str, Any]:
    """Read persisted Workspace files before the model receives its stage input."""
    context = active_invocation_context()
    paths = active_session_paths()
    ledger = ToolCallLedger(paths, context.attempt_id)
    signature = ledger.start(
        "workspace_readme_browser",
        {},
        "Read the current Workspace README and accepted stage files.",
    )
    try:
        raw = workspace_readme_browser.invoke({})
        snapshot = json.loads(str(raw or ""))
        if not isinstance(snapshot, dict) or snapshot.get("ok") is not True:
            raise ValueError("Workspace snapshot is invalid")
    except BaseException:
        ledger.finish(signature, "failed")
        raise
    ledger.finish(signature, "completed")
    return snapshot


def _run_coordinator(
    *,
    stage: str,
    coordinator: Any,
    subagent_id: str,
    registry: Any,
    config: Any,
    message: str,
    thread_suffix: str = "",
) -> tuple[dict[str, Any], WorkerLiveEmitter]:
    try:
        context = active_invocation_context()
        thread_id = f"{context.session_id}:{context.attempt_id}:{stage}"
    except Exception:
        # Unit-level callers without a Session still get a stable repair thread.
        thread_id = f"builder:{stage}:{os.environ.get('HARNESS_RUN_DIR', 'test')}"
    if thread_suffix:
        thread_id = f"{thread_id}:{thread_suffix}"

    def invoke(emitter: WorkerLiveEmitter, payload: str) -> dict[str, Any]:
        message_payload = json.loads(payload)
        if not isinstance(message_payload, dict):
            raise ValueError("Coordinator stage payload must be an object")
        stage_input = message_payload.get("input")
        if not isinstance(stage_input, dict):
            raise ValueError("Coordinator stage input must be an object")
        message_payload["coordination"] = {
            "target_stage": stage,
            "required_subagent": subagent_id,
            "instruction": (
                "Plan this stage. Call task once with required_subagent as subagent_type and a short task description. "
                "The runtime binds the validated current stage input to the task. "
                "Wait for its result before completing. Do not perform specialist work yourself."
            ),
        }
        with bind_delegation_payload(message_payload):
            return stream_agent(
                coordinator,
                harness_root=str(BUILDER_ROOT),
                message=json.dumps(message_payload, ensure_ascii=False),
                registry=registry,
                harness_config=config,
                run_dir=os.environ["HARNESS_RUN_DIR"],
                thread_id=thread_id,
                on_message=emitter.on_message,
                on_stream_chunk=emitter.on_stream_chunk,
                on_subagent_event=emitter.on_subagent_event,
            )

    emitter = WorkerLiveEmitter(stage=stage, run_agent="workspace_builder", sink=emit_worker_event)
    try:
        return invoke(emitter, message), emitter
    except Exception as exc:
        length_limited = type(exc).__name__ == "LengthFinishReasonError"
        if stage not in LENGTH_RETRY_STAGES or not length_limited:
            raise
        if stage == "schema_build":
            retry_limit = int(os.environ.get(SCHEMA_RETRY_OUTPUT_TOKENS_ENV, DEFAULT_SCHEMA_RETRY_OUTPUT_TOKENS))
            feedback = (
                "Previous schema completion hit the output length limit. "
                "Write one compact complete Schema candidate. "
                "Reuse compatible concrete domain entities. Keep distinct domain concepts separate. "
                "Keep unique owner-prefixed relation names."
            )
        elif stage in {"extract", "structured_extract"}:
            retry_limit = int(os.environ.get(EXTRACT_RETRY_OUTPUT_TOKENS_ENV, DEFAULT_EXTRACT_RETRY_OUTPUT_TOKENS))
            feedback = (
                "Previous extraction completion hit the output length limit. "
                "Persist every fact through the draft tools only. "
                "Keep entities and relations in the persisted draft. "
                "Finish after the candidate draft is saved."
            )
        else:
            retry_limit = int(os.environ.get(STAGE_RETRY_OUTPUT_TOKENS_ENV, DEFAULT_STAGE_RETRY_OUTPUT_TOKENS))
            feedback = (
                f"Previous {stage} completion hit the output length limit. "
                "Reuse successful tool results and registered sources from this attempt. "
                "Update the fixed candidate file and finish with a short acknowledgement."
            )
        # Validate the retry budget against the selected model and global cap.
        try:
            from knowcoder_workspace_builder.runtime.model_override import (
                DEEPSEEK_MODELS,
                FRIDAY_MODELS,
                selected_model_ref,
            )

            provider_name, model_id = selected_model_ref().split("/", 1)
            if provider_name == "friday":
                model_cap = int((FRIDAY_MODELS.get(model_id) or {}).get("max_tokens") or DEFAULT_OUTPUT_TOKENS)
            elif provider_name == "deepseek":
                model_cap = int((DEEPSEEK_MODELS.get(model_id) or {}).get("max_tokens") or DEFAULT_OUTPUT_TOKENS)
            else:
                model_cap = DEFAULT_OUTPUT_TOKENS
        except Exception:
            model_cap = int(coordinator.model.max_tokens or DEFAULT_OUTPUT_TOKENS)
        model_cap = min(DEFAULT_OUTPUT_TOKENS, model_cap)
        retry_env = (
            SCHEMA_RETRY_OUTPUT_TOKENS_ENV
            if stage == "schema_build"
            else EXTRACT_RETRY_OUTPUT_TOKENS_ENV
            if stage in {"extract", "structured_extract"}
            else STAGE_RETRY_OUTPUT_TOKENS_ENV
        )
        retry_limit = _configured_output_tokens(retry_env, int(retry_limit), model_cap)
        coordinator.model = replace(coordinator.model, max_tokens=retry_limit)
        stage_input: dict[str, Any] = {}
        try:
            original = json.loads(message)
            if isinstance(original, dict) and isinstance(original.get("input"), dict):
                stage_input = original["input"]
        except json.JSONDecodeError:
            stage_input = {}
        compact_message = json.dumps(
            {
                "stage": stage,
                "input": stage_input,
                "validation_feedback": {
                    "errors": [feedback],
                    "previous_response": "",
                },
            },
            ensure_ascii=False,
        )
        retry_emitter = WorkerLiveEmitter(stage=stage, run_agent="workspace_builder", sink=emit_worker_event)
        return invoke(retry_emitter, compact_message), retry_emitter


def _result_messages(value: dict[str, Any]) -> list[Any]:
    messages = value.get("messages")
    nested = value.get("_subagent_messages")
    return [
        *(list(messages) if isinstance(messages, list) else []),
        *(list(nested) if isinstance(nested, list) else []),
    ]


def _successful_tool_payload(value: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    for message in reversed(_result_messages(value)):
        if not isinstance(message, ToolMessage) or str(message.name or "") != tool_name:
            continue
        try:
            payload = json.loads(str(message.content or ""))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("ok") is not False and not payload.get("error"):
            return payload
    return None


def _latest_tool_failure(value: dict[str, Any], tool_name: str) -> tuple[str, str]:
    """Return the last precise model-facing tool failure type and message."""
    for message in reversed(_result_messages(value)):
        if not isinstance(message, ToolMessage) or str(message.name or "") != tool_name:
            continue
        try:
            payload = json.loads(str(message.content or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        error = str(payload.get("error") or payload.get("validation_error") or "").strip()
        errors = payload.get("errors")
        if not error and isinstance(errors, list):
            error = next((str(item).strip() for item in errors if str(item).strip()), "")
        if error:
            return str(payload.get("error_type") or "tool_error"), error
    return "", ""


def _raise_required_tool_failure(value: dict[str, Any], tool_name: str, fallback: str, *, stage: str) -> None:
    error_type, error = _latest_tool_failure(value, tool_name)
    message = error or fallback
    if error_type == "ExternalServiceError" or error_type in TRANSIENT_EXTERNAL_ERROR_TYPES:
        raise ExternalServiceError(message, stage=stage, error_type=error_type)
    raise ContractError(message, stage=stage, tool=tool_name, error_type=error_type or "missing_success")


def _unrecovered_evidence_service_failure(
    value: dict[str, Any],
    stage_input: dict[str, Any],
) -> str:
    """Return the real search failure when no other evidence source succeeded."""
    if stage_input.get("upload_paths"):
        return ""
    if any(_successful_tool_payload(value, name) is not None for name in ("web_search", "web_search_batch")):
        return ""
    for message in reversed(_result_messages(value)):
        if not isinstance(message, ToolMessage) or str(message.name or "") not in {
            "web_search",
            "web_search_batch",
        }:
            continue
        try:
            payload = json.loads(str(message.content or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("error_type") in {"external_search_error", "tool_circuit_open"}:
            return str(payload.get("error") or "The external evidence search service failed.")
        results = payload.get("results")
        if isinstance(results, list):
            failed = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict)
                    and item.get("error_type") in {"external_search_error", "tool_circuit_open"}
                ),
                None,
            )
            if failed is not None:
                return str(failed.get("error") or "The external evidence search service failed.")
    return ""


def _configure_validation_repair(
    stage: str,
    specialist: Any,
    *,
    reason: str = "",
) -> None:
    """Expose the stage tools needed to repair the current candidate."""
    tools = getattr(specialist, "tools", None)
    if tools is None:
        raise ValueError(f"Builder Subagent {specialist.id} is missing its tool configuration")
    allowed = (
        [STAGE_PERSISTENCE_TOOLS[stage]]
        if reason == "missing_artifact"
        else list(VALIDATION_REPAIR_TOOLS[stage])
    )
    missing = sorted(set(allowed) - set(tools.allow))
    if missing:
        raise ValueError(f"Builder Subagent {specialist.id} is missing repair tools: {', '.join(missing)}")
    tools.allow = allowed


def _validation_repair_message(
    stage: str,
    stage_input: dict[str, Any],
    feedback: dict[str, Any],
) -> str:
    payload: dict[str, Any] = {
        "stage": stage,
        "input": stage_input,
        "validation_feedback": feedback,
        "repair_mode": (
            "create_missing_candidate"
            if feedback.get("context", {}).get("reason") == "missing_artifact"
            else "edit_saved_candidate"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _run_schema_specialist(
    *,
    stage_input: dict[str, Any],
    specialist: Any,
    registry: Any,
    config: Any,
) -> tuple[dict[str, Any], WorkerLiveEmitter]:
    context = active_invocation_context()
    thread_id = f"{context.session_id}:{context.attempt_id}:schema_build"
    emitter = WorkerLiveEmitter(
        stage="schema_build",
        run_agent="workspace_builder",
        sink=emit_worker_event,
    )
    payload = {
        "stage": "schema_build",
        "input": stage_input,
        "execution": {
            "mode": "parallel_evidence_schema",
            "instruction": (
                "Generate candidates from every assigned evidence chunk, resolve the merged semantic conflicts, "
                "save one optimized Schema patch, and finish."
            ),
        },
    }
    result = stream_agent(
        specialist,
        harness_root=str(BUILDER_ROOT),
        message=json.dumps(payload, ensure_ascii=False),
        registry=registry,
        harness_config=config,
        run_dir=os.environ["HARNESS_RUN_DIR"],
        thread_id=thread_id,
        on_message=emitter.on_message,
        on_stream_chunk=emitter.on_stream_chunk,
        on_subagent_event=emitter.on_subagent_event,
    )
    return result, emitter


def _run_schema_stage(
    *,
    stage_input: dict[str, Any],
    specialist: Any,
    registry: Any,
    config: Any,
) -> tuple[dict[str, Any], WorkerLiveEmitter]:
    workflow_started_at = datetime.now(UTC).isoformat()
    _emit_subagent_lifecycle("schema_build", "running", workflow_started_at=workflow_started_at)
    result, emitter = _run_schema_specialist(
        stage_input=stage_input,
        specialist=specialist,
        registry=registry,
        config=config,
    )
    if _successful_tool_payload(result, "build_schema_candidates") is None:
        _raise_required_tool_failure(
            result,
            "build_schema_candidates",
            "Schema Subagent did not build candidates from the assigned evidence chunks",
            stage="schema_build",
        )
    if _successful_tool_payload(result, "save_schema") is None:
        _raise_required_tool_failure(
            result,
            "save_schema",
            "Schema Subagent did not save the optimized Schema",
            stage="schema_build",
        )
    _emit_subagent_lifecycle("schema_build", "validating", workflow_started_at=workflow_started_at)
    return result, emitter


def main() -> None:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or not isinstance(request.get("input"), dict):
            raise ValueError("Harness worker request must contain stage and input objects")
        stage = str(request.get("stage") or "")
        if not stage.strip():
            raise ValueError("Harness worker request requires a stage")
        config, registry = load_harness_registry()
        specialist = _specialist_for_stage(stage, registry)
        _configure_specialist(stage, specialist, request["input"])
        coordinator = _coordinator_for_stage(stage, registry, specialist)
        workspace_snapshot = {} if stage == "schema_build" else _load_workspace_snapshot()
        model_input = (
            dict(request["input"])
            if stage == "schema_build"
            else {**request["input"], "workspace_snapshot": workspace_snapshot}
        )
        message = json.dumps({"stage": stage, "input": model_input}, ensure_ascii=False)
        os.environ[VALIDATION_ROUND_ENV] = "1"
        if stage == "schema_build":
            result, live_emitter = _run_schema_stage(
                stage_input=request["input"],
                specialist=specialist,
                registry=registry,
                config=config,
            )
        else:
            result, live_emitter = _run_coordinator(
                stage=stage,
                coordinator=coordinator,
                subagent_id=specialist.id,
                registry=registry,
                config=config,
                message=message,
            )
        if stage == "extract" and _successful_tool_payload(result, "extract_unstructured_chunks") is None:
            _raise_required_tool_failure(
                result,
                "extract_unstructured_chunks",
                "Unstructured extraction tool did not complete successfully",
                stage=stage,
            )
        if stage != "schema_build":
            _emit_subagent_lifecycle(stage, "validating")
        model_snapshot = live_emitter.snapshot()
        model_snapshot.pop("completion_output", None)
        round_index = 1
        while True:
            os.environ[VALIDATION_ROUND_ENV] = str(round_index)
            validation = validate_current_artifact(
                stage,
                stage_input=request["input"],
                validation_round=round_index,
            )
            if round_index > 1:
                _emit_validation_repair(
                    stage,
                    round_index,
                    "done" if validation.ok else "failed",
                )
            if (
                stage == "evidence"
                and not validation.ok
                and validation.outcome.context.get("reason") == "missing_artifact"
            ):
                service_error = _unrecovered_evidence_service_failure(result, request["input"])
                if service_error:
                    raise ExternalServiceError(service_error, stage=stage)
            if validation.ok or not validation.retryable or round_index >= MAX_VALIDATION_ROUNDS:
                stage_result = validation.to_stage_result(stage_input=request["input"]).to_dict(include_private=True)
                break
            feedback = {
                **validation.feedback(),
                "round": round_index,
                "max_rounds": MAX_VALIDATION_ROUNDS,
                "repair_instruction": (
                    "Collect required evidence, create the missing candidate, and save it."
                    if stage == "evidence"
                    and validation.outcome.context.get("reason") == "missing_artifact"
                    else "Create and save the complete missing candidate."
                    if validation.outcome.context.get("reason") == "missing_artifact"
                    else "Read the saved candidate, repair the listed errors, and save the same file again."
                ),
            }
            repair_message = _validation_repair_message(
                stage,
                model_input,
                feedback,
            )
            _configure_validation_repair(
                stage,
                specialist,
                reason=str(validation.outcome.context.get("reason") or ""),
            )
            _emit_validation_repair(stage, round_index + 1, "running")
            _emit_subagent_lifecycle(stage, "running")
            result, repair_emitter = _run_coordinator(
                stage=stage,
                coordinator=coordinator,
                subagent_id=specialist.id,
                registry=registry,
                config=config,
                message=repair_message,
                thread_suffix=f"validation-{round_index + 1}",
            )
            _emit_subagent_lifecycle(stage, "validating")
            model_snapshot = repair_emitter.snapshot()
            model_snapshot.pop("completion_output", None)
            round_index += 1
        response = {
            "ok": True,
            "result": stage_result,
            "model": model_snapshot,
        }
    except Exception as exc:  # noqa: BLE001 - process boundary reports the real error type.
        response = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
