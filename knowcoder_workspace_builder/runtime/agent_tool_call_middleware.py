"""Middleware classes referenced by the protected Builder Harness configuration."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from knowcoder_workspace_builder.contracts.errors import BuilderError, ContractError
from knowcoder_workspace_builder.storage.attempts import AttemptStore
from knowcoder_workspace_builder.storage.paths import ProjectLayout
from knowcoder_workspace_builder.storage.stage_artifacts import artifact_path
from knowcoder_workspace_builder.storage.tool_calls import (
    FetchLedger,
    SearchLedger,
    ToolCallLedger,
)

from .invocation_context import active_invocation_context
from .session_context import ATTEMPT_ID_ENV, active_session_paths
from .virtual_paths import resolve_virtual_path

VALIDATION_ROUND_ENV = "SCHEMA_VALIDATION_ROUND"


def _tool_error(request: ToolCallRequest, error_type: str, message: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"ok": False, "error_type": error_type, "error": message}, ensure_ascii=False),
        name=str(request.tool_call.get("name") or ""),
        tool_call_id=request.tool_call["id"],
        status="error",
    )


def _json_payload(message: ToolMessage) -> dict[str, Any] | None:
    try:
        value = json.loads(str(message.content or ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    nested = value.get("stage_result")
    return dict(nested) if isinstance(nested, dict) else value


def _state_messages(state: Any) -> list[Any]:
    if isinstance(state, dict):
        value = state.get("messages")
    else:
        value = getattr(state, "messages", None)
        if value is None:
            try:
                value = state["messages"]
            except (KeyError, TypeError):
                value = None
    return list(value) if isinstance(value, (list, tuple)) else []


def _current_model_turn_messages(messages: list[Any]) -> list[Any]:
    """Return only messages produced after the latest human instruction."""
    start = 0
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage) or str(getattr(message, "type", "") or "") == "human":
            start = index + 1
    return messages[start:]


class PersistenceDoneStopMiddleware(AgentMiddleware):
    """Stop after a successful stage persistence tool so validation can run.

    File-backed stages write candidates through fixed tools. Once that tool
    returns ok=true, extra model turns only risk hanging or rewriting the file.
    The outer harness still validates the saved candidate and can request repair.
    """

    _STAGE_TOOLS = {
        "problem": "save_problem_review",
        "schema_judge": "save_schema_judgement",
        "extract": "extract_unstructured_chunks",
        "structured_extract": "append_instances_batches_from_file",
        "document": "save_workspace_readme",
    }
    _FAILURE_TOOLS = {
        **_STAGE_TOOLS,
        "evidence": "save_evidence_manifest",
        "schema_build": "save_schema",
    }
    MAX_FAILURES_PER_TURN = 2

    def _stop(self, request: ModelRequest) -> ModelResponse | None:
        try:
            stage = active_invocation_context().stage
        except Exception:
            return None
        stage = str(stage or "")
        tool_name = self._FAILURE_TOOLS.get(stage)
        if not tool_name:
            return None
        messages = _current_model_turn_messages(list(request.messages or []))
        failures = 0
        for message in reversed(messages):
            if not isinstance(message, ToolMessage) or message.name != tool_name:
                continue
            payload = _json_payload(message)
            if payload is None:
                return None
            if payload.get("ok") is True:
                if stage not in self._STAGE_TOOLS:
                    return None
                return ModelResponse(
                    result=[
                        AIMessage(
                            content=(
                                f"{tool_name} saved the candidate successfully. "
                                "Validation will inspect the saved file next."
                            )
                        )
                    ]
                )
            failures += 1
            if failures >= self.MAX_FAILURES_PER_TURN:
                return ModelResponse(
                    result=[
                        AIMessage(
                            content=(
                                f"{tool_name} failed {failures} times in this validation round. "
                                "Stop this model turn so deterministic validation can start a bounded repair round."
                            )
                        )
                    ]
                )
        return None

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._stop(request) or handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._stop(request) or await handler(request)


class StageCompletionContractMiddleware(AgentMiddleware):
    """Require each Builder specialist to complete its declared durable action."""

    _REQUIRED_TOOLS = {
        "problem": "save_problem_review",
        "evidence": "save_evidence_manifest",
        "schema_build": "save_schema",
        "schema_judge": "save_schema_judgement",
        "extract": "extract_unstructured_chunks",
        "structured_extract": "append_instances_batches_from_file",
        "document": "save_workspace_readme",
    }
    _CORRECTION_MARKER = "[builder-stage-completion]"
    MAX_CORRECTIONS = 2
    MAX_PERSISTENCE_FAILURES = 2

    @staticmethod
    def _successful(messages: list[Any], tool_name: str) -> bool:
        return any(
            isinstance(message, ToolMessage)
            and str(message.name or "") == tool_name
            and (payload := _json_payload(message)) is not None
            and payload.get("ok") is True
            for message in messages
        )

    @staticmethod
    def _failure_count(messages: list[Any], tool_name: str) -> int:
        return sum(
            1
            for message in messages
            if isinstance(message, ToolMessage)
            and str(message.name or "") == tool_name
            and (payload := _json_payload(message)) is not None
            and payload.get("ok") is False
        )

    @classmethod
    def _correction_count(cls, messages: list[Any]) -> int:
        return sum(
            cls._CORRECTION_MARKER in str(getattr(message, "content", "") or "")
            for message in messages
            if isinstance(message, SystemMessage)
        )

    @staticmethod
    def _evidence_circuit_open(messages: list[Any]) -> bool:
        for message in messages:
            if not isinstance(message, ToolMessage) or str(message.name or "") not in {
                "web_search",
                "web_search_batch",
            }:
                continue
            payload = _json_payload(message) or {}
            if payload.get("error_type") == "tool_circuit_open":
                return True
            results = payload.get("results")
            if isinstance(results, list) and any(
                isinstance(item, dict) and item.get("error_type") == "tool_circuit_open"
                for item in results
            ):
                return True
        return False

    @staticmethod
    def _has_successful_search(messages: list[Any]) -> bool:
        return any(
            isinstance(message, ToolMessage)
            and str(message.name or "") in {"web_search", "web_search_batch"}
            and (payload := _json_payload(message)) is not None
            and payload.get("ok") is True
            for message in messages
        )

    def _instruction(self, stage: str, messages: list[Any], tool_name: str) -> str:
        if stage == "evidence" and not self._has_successful_search(messages):
            action = (
                "Call web_search_batch now for the uncovered research steps. "
                "Fetch promising links, select only relevant candidate IDs, and call save_evidence_manifest."
            )
        elif stage == "schema_build" and not self._successful(messages, "build_schema_candidates"):
            action = (
                "Call build_schema_candidates once now. Review the merged candidate and conflicts, "
                "then call save_schema with the optimized Schema."
            )
        else:
            action = f"Call {tool_name} now with the complete stage result."
        return (
            f"{self._CORRECTION_MARKER} The stage is incomplete because {tool_name} has not succeeded. "
            f"{action} Finish only after the tool reports ok=true."
        )

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        try:
            stage = str(active_invocation_context().stage or "")
        except Exception:
            return None
        tool_name = self._REQUIRED_TOOLS.get(stage)
        if not tool_name:
            return None
        messages = _current_model_turn_messages(_state_messages(state))
        last_ai = next((message for message in reversed(messages) if isinstance(message, AIMessage)), None)
        if last_ai is None or last_ai.tool_calls:
            return None
        if self._successful(messages, tool_name):
            return None
        if self._failure_count(messages, tool_name) >= self.MAX_PERSISTENCE_FAILURES:
            return None
        if stage == "evidence" and self._evidence_circuit_open(messages):
            return None
        if self._correction_count(messages) >= self.MAX_CORRECTIONS:
            return None
        return {
            "messages": [SystemMessage(content=self._instruction(stage, messages, tool_name))],
            "jump_to": "model",
        }

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


class StructuredExtractProgressMiddleware(AgentMiddleware):
    """Keep structured extraction moving after sources and schema are loaded.

    Real hangs often happen after source_reader/get_schema_outline succeed but
    before write_file/execute_code/append. Guide the model once, then force a
    skip-capable completion path if persistence still never starts.
    """

    def _counts(self, messages: list[Any]) -> dict[str, int]:
        counts = {
            "source_reader_ok": 0,
            "schema_outline_ok": 0,
            "write_file": 0,
            "execute_code": 0,
            "append": 0,
            "assistant_after_read": 0,
        }
        saw_read = False
        for message in messages:
            if isinstance(message, ToolMessage):
                payload = _json_payload(message) or {}
                name = str(message.name or "")
                if name == "source_reader" and payload.get("ok") is True:
                    counts["source_reader_ok"] += 1
                    saw_read = True
                elif name == "get_schema_outline" and payload.get("ok") is True:
                    counts["schema_outline_ok"] += 1
                    saw_read = True
                elif name == "write_file":
                    counts["write_file"] += 1
                elif name == "execute_code":
                    counts["execute_code"] += 1
                elif name == "append_instances_batches_from_file":
                    counts["append"] += 1
            elif saw_read and isinstance(message, AIMessage):
                counts["assistant_after_read"] += 1
        return counts

    def _guide(self, request: ModelRequest) -> ModelRequest | ModelResponse | None:
        try:
            if active_invocation_context().stage != "structured_extract":
                return None
        except Exception:
            return None
        counts = self._counts(list(request.messages or []))
        if counts["append"] > 0 or counts["write_file"] > 0 or counts["execute_code"] > 0:
            return None
        if counts["source_reader_ok"] == 0 or counts["schema_outline_ok"] == 0:
            return None
        guide = SystemMessage(
            content=(
                "Sources and schema outline are already loaded. "
                "Write the parser script, execute it, and call append_instances_batches_from_file now. "
                "Do not reread sources."
            )
        )
        return request.override(messages=[*list(request.messages or []), guide])

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        guided = self._guide(request)
        if isinstance(guided, ModelResponse):
            return guided
        return handler(guided or request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        guided = self._guide(request)
        if isinstance(guided, ModelResponse):
            return guided
        return await handler(guided or request)


class RunAttemptGuardMiddleware(AgentMiddleware):
    """Reject tool calls from missing, cancelled, or already finished attempts."""

    _OBJECTIVES = {
        "workspace_readme_browser": "Read current Workspace metadata needed for problem clarification.",
        "source_reader": "Read the sources assigned to the current stage.",
        "web_search": "Resolve one declared evidence coverage gap.",
        "web_search_batch": "Resolve multiple declared evidence coverage gaps in one batch.",
        "fetch_web_pages": "Fetch complete content for explicit evidence URLs.",
        "save_problem_review": "Persist the complete problem decomposition candidate.",
        "save_evidence_manifest": "Persist completed evidence coverage and provenance.",
        "build_schema_candidates": "Generate and merge Schema candidates from the assigned evidence chunks.",
        "save_schema": "Persist a semantic Schema batch for runtime compilation.",
        "save_schema_judgement": "Persist the complete schema review decision.",
        "save_workspace_readme": "Persist the complete Workspace README candidate.",
        "schema_validator": "Validate the current schema candidate once.",
        "get_schema_outline": "Read exact schema names for extraction.",
        "extract_unstructured_chunks": "Extract every chunk assigned to the current unstructured stage.",
        "append_instances_batch": "Persist one grounded unstructured extraction batch.",
        "append_instances_batches_from_file": "Persist the deterministic structured extraction batch.",
        "write_file": "Write the current structured extraction script or batch.",
        "execute_code": "Execute the current structured extraction script.",
    }

    def _prepare(self, request: ToolCallRequest) -> tuple[ToolMessage | None, ToolCallLedger | None, str]:
        attempt_id = os.environ.get(ATTEMPT_ID_ENV, "").strip()
        try:
            paths = active_session_paths()
            context = active_invocation_context()
            if not attempt_id:
                raise ContractError("Active attempt ID is missing")
            store = AttemptStore(ProjectLayout(paths.project))
            if not store.is_active(paths.session_id, attempt_id):
                raise ContractError("Active attempt is no longer running", attempt_id=attempt_id)
            tool_name = str(request.tool_call.get("name") or "")
            arguments = request.tool_call.get("args")
            arguments = arguments if isinstance(arguments, dict) else {}
            validation_round = int(os.environ.get(VALIDATION_ROUND_ENV, "1"))
            objective = str(arguments.get("purpose") or self._OBJECTIVES.get(tool_name) or "").strip()
            ledger = ToolCallLedger(paths, attempt_id)
            if context.stage == "evidence" and tool_name in {"web_search", "fetch_web_pages"}:
                workspace_context = context.input.get("workspace_context")
                uncovered_step_indexes = (
                    workspace_context.get("uncovered_step_indexes")
                    if isinstance(workspace_context, dict)
                    else None
                )
                if not isinstance(uncovered_step_indexes, list) or any(
                    not isinstance(index, int) or isinstance(index, bool) or index < 1
                    for index in uncovered_step_indexes
                ):
                    raise ContractError(
                        "Evidence search requires valid workspace_context.uncovered_step_indexes"
                    )
                step_index = arguments.get("step_index")
                if not isinstance(step_index, int) or isinstance(step_index, bool):
                    raise ContractError(f"{tool_name} requires an integer step_index")
                if step_index not in uncovered_step_indexes:
                    raise ContractError(
                        f"{tool_name} step_index is outside the current uncovered research steps",
                        step_index=step_index,
                        uncovered_step_indexes=uncovered_step_indexes,
                    )
            if (
                context.stage == "extract"
                and tool_name == "source_reader"
                and ledger.completed_count("source_reader")
                > ledger.completed_count("append_instances_batch")
                and validation_round <= 1
            ):
                raise ContractError(
                    "Persist the current unstructured source batch before reading the next batch"
                )
            if (
                context.stage == "structured_extract"
                and tool_name in {"write_file", "execute_code"}
                and ledger.completed_count("execute_code")
                > ledger.finished_count("append_instances_batches_from_file")
            ):
                raise ContractError(
                    "Call append_instances_batches_from_file before another file action"
                )
            ledger_arguments = arguments
            if context.stage == "evidence" and tool_name == "save_evidence_manifest":
                paths = active_session_paths()
                ledger_arguments = {
                    **arguments,
                    "_runtime_research_revision": {
                        "searches": len(SearchLedger(paths, attempt_id).records()),
                        "fetches": len(FetchLedger(paths, attempt_id).records()),
                    },
                }
            if context.stage == "structured_extract" and tool_name == "append_instances_batches_from_file":
                batch_path = artifact_path(paths, attempt_id, "structured_batches")
                batch_revision = (
                    hashlib.sha256(batch_path.read_bytes()).hexdigest()
                    if batch_path.is_file()
                    else "missing"
                )
                ledger_arguments = {
                    **arguments,
                    "_runtime_batch_revision": batch_revision,
                }
            if context.stage == "extract" and tool_name == "source_reader" and validation_round > 1:
                ledger_arguments = {**arguments, "_validation_round": validation_round}
            signature = ledger.start(tool_name, ledger_arguments, objective)
        except (BuilderError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return _tool_error(request, "invalid_tool_call", str(exc)), None, ""
        return None, ledger, signature

    @staticmethod
    def _status(value: ToolMessage | Command) -> str:
        if isinstance(value, ToolMessage):
            if value.status == "error":
                return "failed"
            payload = _json_payload(value)
            if payload:
                status = str(payload.get("status") or "").strip().casefold()
                if (
                    payload.get("ok") is False
                    or payload.get("error")
                    or payload.get("error_type")
                    or status in {"error", "failed", "failure"}
                ):
                    return "failed"
        return "completed"

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        error, ledger, signature = self._prepare(request)
        if error is not None:
            return error
        assert ledger is not None
        try:
            result = handler(request)
        except BaseException:
            ledger.finish(signature, "failed")
            raise
        ledger.finish(signature, self._status(result))
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        error, ledger, signature = self._prepare(request)
        if error is not None:
            return error
        assert ledger is not None
        try:
            result = await handler(request)
        except BaseException:
            ledger.finish(signature, "failed")
            raise
        ledger.finish(signature, self._status(result))
        return result


class EvidenceManifestPreflightMiddleware(AgentMiddleware):
    """Check evidence coverage fields declared by the Evidence Collector Prompt."""

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = request.tool_call.get("name")
        if tool_name == "source_reader":
            try:
                context = active_invocation_context()
            except BuilderError:
                return None
            if context.stage == "evidence" and not context.input.get("upload_paths"):
                return ToolMessage(
                    content=json.dumps(
                        {
                            "ok": True,
                            "sources": [],
                            "message": (
                                "No uploads were supplied. Discover candidates with Search, review complete bodies with Fetch, "
                                "then select relevant candidate IDs in save_evidence_manifest."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    name="source_reader",
                    tool_call_id=request.tool_call["id"],
                )
        if tool_name != "save_evidence_manifest":
            return None
        args = request.tool_call.get("args")
        if not isinstance(args, dict):
            return _tool_error(request, "invalid_evidence_manifest", "Evidence manifest arguments must be an object")
        for field in ("coverage", "unresolved_gaps"):
            if not isinstance(args.get(field), list):
                return _tool_error(request, "invalid_evidence_manifest", f"{field} must be a list")
        if "selected_web_sources" in args and not isinstance(args.get("selected_web_sources"), list):
            return _tool_error(request, "invalid_evidence_manifest", "selected_web_sources must be a list")
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        error = self._check(request)
        if error is not None:
            result: ToolMessage | Command = error
        else:
            result = handler(request)
        return result

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        error = self._check(request)
        if error is not None:
            result: ToolMessage | Command = error
        else:
            result = await handler(request)
        return result

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return await handler(request)


class FailedToolCircuitBreakerMiddleware(AgentMiddleware):
    _EVIDENCE_REASONING_GUIDE = (
        "Keep private reasoning_content to exactly three short one-sentence lines: "
        "Need states what the research must establish; Searched states what completed searches established; "
        "Missing states what evidence still needs collection. "
        "Start each line with its label and keep all three lines within 300 characters total. "
        "Put source names, URLs, comparisons, and selection details in tool arguments. "
        "Then make the next Search, Fetch, or Save tool call immediately with empty assistant content."
    )

    @classmethod
    def _with_evidence_guide(cls, request: ModelRequest, action: str) -> ModelRequest:
        current = getattr(request, "system_message", None)
        base = str(getattr(current, "content", "") or "")
        content = "\n\n".join(part for part in (base, action, cls._EVIDENCE_REASONING_GUIDE) if part)
        return request.override(system_message=SystemMessage(content=content))

    """Allow limited external-search retries before opening the circuit.

    Prompt policy: a failed search step may be retried with corrected input up to
    three failed external attempts. Only after that many external_search_error
    results do we block further web_search calls and force stage completion.
    Invalid request errors never open the circuit.
    """

    MAX_EXTERNAL_SEARCH_FAILURES = 3

    def __init__(self, tool_names: list[str]) -> None:
        self.tool_names = set(tool_names)

    @staticmethod
    def _is_external_failure(payload: dict[str, Any]) -> bool:
        if payload.get("error_type") == "external_search_error":
            return True
        results = payload.get("results")
        return isinstance(results, list) and any(
            isinstance(item, dict) and item.get("error_type") == "external_search_error"
            for item in results
        )

    def _external_failure_count(self, messages: list[Any], tool_name: str) -> int:
        count = 0
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name != tool_name:
                continue
            payload = _json_payload(message)
            if payload and self._is_external_failure(payload):
                count += 1
        return count

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        name = str(request.tool_call.get("name") or "")
        if name not in self.tool_names:
            return None
        messages = _state_messages(request.state)
        search_tools = self.tool_names.intersection({"web_search", "web_search_batch"})
        failures = (
            sum(self._external_failure_count(messages, tool_name) for tool_name in search_tools)
            if name in search_tools
            else self._external_failure_count(messages, name)
        )
        if failures < self.MAX_EXTERNAL_SEARCH_FAILURES:
            return None
        return _tool_error(
            request,
            "tool_circuit_open",
            (
                f"{name} failed {failures} times in this invocation "
                f"(limit {self.MAX_EXTERNAL_SEARCH_FAILURES}). "
                "Finish with available sources or mark remaining steps limited."
            ),
        )

    def _circuit_state(self, messages: list[Any]) -> tuple[int, int]:
        search_tools = self.tool_names.intersection({"web_search", "web_search_batch"})
        if not search_tools:
            return 0, 0
        circuit_count = 0
        external_failures = sum(self._external_failure_count(messages, name) for name in search_tools)
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name not in search_tools:
                continue
            payload = _json_payload(message)
            if not payload:
                continue
            if payload.get("error_type") == "tool_circuit_open":
                circuit_count += 1
        return circuit_count, external_failures

    def _terminal_failure(self, messages: list[Any]) -> ModelResponse | None:
        circuit_count, external_failures = self._circuit_state(messages)
        # Only force-stop after the external-failure retry budget is exhausted and
        # the circuit has actually opened at least once.
        if external_failures < self.MAX_EXTERNAL_SEARCH_FAILURES:
            return None
        if not circuit_count:
            return None
        error = (
            "The data source service failed repeatedly during this invocation "
            f"after {external_failures} external search failures."
        )
        return ModelResponse(result=[AIMessage(content=error)])

    def _guide_search_completion(self, request: ModelRequest) -> ModelRequest:
        try:
            context = active_invocation_context()
            if context.stage != "evidence":
                return request
            workspace_context = context.input.get("workspace_context")
            configured_indexes = (
                workspace_context.get("uncovered_step_indexes")
                if isinstance(workspace_context, dict)
                else None
            )
            indexes = (
                [int(item) for item in configured_indexes if isinstance(item, int)]
                if isinstance(configured_indexes, list)
                else list(range(1, len(context.input.get("steps") or []) + 1))
            )
            completed_by_step = dict.fromkeys(indexes, 0)
            records = SearchLedger(active_session_paths(), context.attempt_id).records()
            for record in records:
                step_index = record.get("step_index")
                if record.get("status") == "completed" and step_index in completed_by_step:
                    completed_by_step[step_index] += 1
        except (BuilderError, KeyError, TypeError, ValueError):
            return request
        if indexes and all(completed_by_step[index] >= 1 for index in indexes):
            return self._with_evidence_guide(
                request,
                "Every uncovered step has first-pass Search candidates. "
                "Fetch promising links and keep only pages whose body directly supports the step. "
                "Run focused Search and Fetch calls for material gaps, then save selected candidate IDs.",
            )
        closed = [index for index in indexes if completed_by_step[index] >= 1]
        remaining_focus = [index for index in indexes if completed_by_step[index] < 1]
        if closed:
            return self._with_evidence_guide(
                request,
                f"First-pass Search is complete for step indexes {closed}. "
                f"Search step indexes {remaining_focus}, then Fetch promising links for every step.",
            )
        return self._with_evidence_guide(request, "")

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._terminal_failure(list(request.messages or [])) or handler(self._guide_search_completion(request))

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._terminal_failure(list(request.messages or [])) or await handler(self._guide_search_completion(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return self._check(request) or handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        return self._check(request) or await handler(request)


class RunScopedFileToolMiddleware(AgentMiddleware):
    """Keep structured-extraction scripts inside current Session source storage."""

    _FILE_TOOLS = frozenset({"write_file", "execute_code"})
    _SESSION_WRITE_DIRS = frozenset({"intermediate"})

    def __init__(self, allowed_subdirs: list[str], execute_subdirs: list[str]) -> None:
        self.allowed = {Path(value).parts[0] for value in allowed_subdirs}.intersection(self._SESSION_WRITE_DIRS)
        self.executable = {Path(value).parts[0] for value in execute_subdirs}.intersection(self.allowed)

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        name = str(request.tool_call.get("name") or "")
        if name not in self._FILE_TOOLS:
            return None
        args = request.tool_call.get("args")
        value = args.get("file_path") if isinstance(args, dict) else ""
        try:
            target = resolve_virtual_path(str(value or ""))
            relative = target.relative_to(active_session_paths().root)
            allowed = self.executable if name == "execute_code" else self.allowed
            if not relative.parts or relative.parts[0] not in allowed:
                raise ContractError(f"{name} path is outside its allowed Session directories")
            if name == "write_file" and target.suffix.casefold() != ".py":
                raise ContractError("write_file must create a Python parsing script during structured extraction")
        except (BuilderError, OSError, ValueError) as exc:
            return _tool_error(request, "invalid_run_path", str(exc))
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return self._check(request) or handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        return self._check(request) or await handler(request)
