"""Validate Coordinator task delegation without performing specialist work."""

from __future__ import annotations

import json
from collections.abc import Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from knowcoder_workspace_builder.runtime.invocation_context import (
    active_delegation_payload,
)
from knowcoder_workspace_builder.validation.inputs import validate_stage_input
from knowcoder_workspace_builder.validation.stage_results import STAGE_PROTOCOLS


def _error(request: ToolCallRequest, message: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"ok": False, "error_type": "invalid_delegation", "error": message}),
        tool_call_id=request.tool_call["id"],
        status="error",
    )


def _task_payload(request: ToolCallRequest) -> tuple[str, dict[str, object]]:
    args = request.tool_call.get("args")
    if not isinstance(args, dict):
        raise TypeError("task arguments must be an object")
    subagent = str(args.get("subagent_type") or "").strip()
    if not subagent:
        raise ValueError("task subagent_type must be non-empty")
    payload = active_delegation_payload()
    return subagent, payload


def _bind_task_payload(request: ToolCallRequest, payload: dict[str, object]) -> ToolCallRequest:
    args = request.tool_call.get("args")
    if not isinstance(args, dict):
        raise TypeError("task arguments must be an object")
    # Coordination belongs to the parent Agent. Passing it to the delegated
    # specialist makes the specialist try to delegate its own assigned work.
    specialist_payload = {key: value for key, value in payload.items() if key != "coordination"}
    bound_call = {
        **request.tool_call,
        "args": {
            **args,
            "description": json.dumps(specialist_payload, ensure_ascii=False),
        },
    }
    return request.override(tool_call=bound_call)


class DelegationDisciplineMiddleware(AgentMiddleware):
    """Bind the trusted runtime stage input to a Coordinator task call."""

    def _prepare(self, request: ToolCallRequest) -> ToolCallRequest | ToolMessage:
        if request.tool_call.get("name") != "task":
            return request
        try:
            _, payload = _task_payload(request)
            stage = str(payload.get("stage") or "").strip()
            stage_input = payload.get("input")
            validate_stage_input(stage, stage_input)
            return _bind_task_payload(request, payload)
        except (TypeError, ValueError, RuntimeError) as exc:
            return _error(request, str(exc))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, ToolMessage) else handler(prepared)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, ToolMessage) else await handler(prepared)


class WorkspaceExtractionDelegationMiddleware(AgentMiddleware):
    """Require each stage to be delegated to its sole owning Subagent."""

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        if request.tool_call.get("name") != "task":
            return None
        try:
            subagent, payload = _task_payload(request)
            stage = str(payload.get("stage") or "").strip()
            protocol = STAGE_PROTOCOLS.get(stage)
            if protocol is None:
                raise ValueError(f"unknown Builder stage: {stage}")
            if subagent != protocol.agent:
                raise ValueError(f"stage {stage} must be delegated to {protocol.agent}, not {subagent}")
        except (TypeError, ValueError) as exc:
            return _error(request, str(exc))
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return self._check(request) or handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage | Command:
        return self._check(request) or await handler(request)


class DelegationDoneStopMiddleware(AgentMiddleware):
    """Return control after the stage's single delegated specialist finishes."""

    @staticmethod
    def _delegation_finished(request: ModelRequest) -> bool:
        messages = list(request.messages or [])
        latest_human = -1
        for index, message in enumerate(messages):
            if isinstance(message, HumanMessage) or str(getattr(message, "type", "") or "") == "human":
                latest_human = index
        return any(
            isinstance(message, ToolMessage) and str(message.name or "") == "task"
            for message in messages[latest_human + 1 :]
        )

    def _stop(self, request: ModelRequest) -> ModelResponse | None:
        if not self._delegation_finished(request):
            return None
        return ModelResponse(
            result=[AIMessage(content="The delegated stage finished. Deterministic validation runs next.")]
        )

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._stop(request) or handler(request)

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        return self._stop(request) or await handler(request)
