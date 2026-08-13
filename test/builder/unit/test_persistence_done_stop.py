"""Successful persistence tools should stop the model turn promptly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from knowcoder_workspace_builder.runtime.agent_tool_call_middleware import (
    PersistenceDoneStopMiddleware,
    StageCompletionContractMiddleware,
)
from knowcoder_workspace_builder.runtime.invocation_context import InvocationContext


class _Req:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    def override(self, *, messages: list[Any]):
        return _Req(messages)


def test_persistence_done_allows_incremental_schema_batches() -> None:
    middleware = PersistenceDoneStopMiddleware()
    request = _Req(
        [
            HumanMessage(content="build schema"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "save_schema", "args": {}}]),
            ToolMessage(name="save_schema", content='{"ok": true, "candidate_path": "x"}', tool_call_id="1"),
        ]
    )

    called = {"n": 0}

    def handler(_req):
        called["n"] += 1
        return SimpleNamespace(result=[AIMessage(content="continue schema batches")])

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="schema_build", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(request, handler)
    assert called["n"] == 1
    assert response.result[0].content == "continue schema batches"


def test_persistence_done_allows_evidence_search_after_a_successful_snapshot() -> None:
    middleware = PersistenceDoneStopMiddleware()
    request = _Req(
        [
            HumanMessage(content="collect evidence"),
            ToolMessage(
                name="save_evidence_manifest",
                content='{"ok": true, "candidate_path": "evidence.json"}',
                tool_call_id="1",
            ),
        ]
    )
    called = {"n": 0}

    def handler(_req):
        called["n"] += 1
        return SimpleNamespace(result=[AIMessage(content="review coverage and continue if needed")])

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="evidence", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(request, handler)

    assert called["n"] == 1
    assert response.result[0].content == "review coverage and continue if needed"


def test_persistence_done_does_not_stop_on_failed_save() -> None:
    middleware = PersistenceDoneStopMiddleware()
    request = _Req(
        [ToolMessage(name="save_schema", content='{"ok": false, "error": "bad"}', tool_call_id="1")]
    )
    called = {"n": 0}

    def handler(_req):
        called["n"] += 1
        return SimpleNamespace(result=[AIMessage(content="continue")])

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="schema_build", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(request, handler)
    assert called["n"] == 1
    assert response.result[0].content == "continue"


def test_persistence_done_stops_repeated_failed_saves_for_bounded_repair() -> None:
    middleware = PersistenceDoneStopMiddleware()
    request = _Req(
        [
            ToolMessage(name="save_evidence_manifest", content='{"ok": false, "error": "no sources"}', tool_call_id="1"),
            ToolMessage(name="save_evidence_manifest", content='{"ok": false, "error": "no sources"}', tool_call_id="2"),
        ]
    )

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="evidence", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(
            request,
            lambda _req: (_ for _ in ()).throw(AssertionError("repeated failures must stop the turn")),
        )

    assert "failed 2 times" in response.result[0].content
    assert "bounded repair round" in response.result[0].content


def test_persistence_done_ignores_success_before_latest_repair_instruction() -> None:
    middleware = PersistenceDoneStopMiddleware()
    request = _Req(
        [
            HumanMessage(content="build schema"),
            AIMessage(content="", tool_calls=[{"id": "1", "name": "save_schema", "args": {}}]),
            ToolMessage(name="save_schema", content='{"ok": true}', tool_call_id="1"),
            HumanMessage(content="repair the saved schema"),
        ]
    )
    called = {"n": 0}

    def handler(_req):
        called["n"] += 1
        return SimpleNamespace(result=[AIMessage(content="repairing")])

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="schema_build", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(request, handler)

    assert called["n"] == 1
    assert response.result[0].content == "repairing"


def test_structured_extract_progress_keeps_real_persistence_path_after_idle_turns() -> None:
    from knowcoder_workspace_builder.runtime.agent_tool_call_middleware import StructuredExtractProgressMiddleware
    middleware = StructuredExtractProgressMiddleware()
    request = _Req(
        [
            ToolMessage(name="source_reader", content='{"ok": true}', tool_call_id="1"),
            ToolMessage(name="get_schema_outline", content='{"ok": true}', tool_call_id="2"),
            AIMessage(content="thinking"),
            AIMessage(content="still thinking"),
        ]
    )

    seen = {}

    def handler(guided_request):
        seen["messages"] = guided_request.messages
        return SimpleNamespace(result=[AIMessage(content="continue")])

    ctx = InvocationContext(session_id="s", attempt_id="a", stage="structured_extract", input={})
    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        response = middleware.wrap_model_call(request, handler)
    assert response.result[0].content == "continue"
    assert "append_instances_batches_from_file now" in seen["messages"][-1].content


def test_stage_completion_reopens_model_when_required_tool_was_not_called() -> None:
    middleware = StageCompletionContractMiddleware()
    state = {"messages": [HumanMessage(content="collect"), AIMessage(content="I will search now.")]}
    ctx = InvocationContext(session_id="s", attempt_id="a", stage="evidence", input={})

    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        update = middleware.after_model(state, SimpleNamespace())

    assert update is not None
    assert update["jump_to"] == "model"
    assert "web_search_batch" in update["messages"][0].content
    assert "save_evidence_manifest" in update["messages"][0].content


def test_stage_completion_allows_successful_required_tool() -> None:
    middleware = StageCompletionContractMiddleware()
    state = {
        "messages": [
            HumanMessage(content="collect"),
            ToolMessage(name="save_evidence_manifest", content='{"ok": true}', tool_call_id="1"),
            AIMessage(content="done"),
        ]
    }
    ctx = InvocationContext(session_id="s", attempt_id="a", stage="evidence", input={})

    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        assert middleware.after_model(state, SimpleNamespace()) is None


def test_stage_completion_stops_after_bounded_corrections() -> None:
    middleware = StageCompletionContractMiddleware()
    state = {
        "messages": [
            HumanMessage(content="collect"),
            SystemMessage(content="[builder-stage-completion] first"),
            SystemMessage(content="[builder-stage-completion] second"),
            AIMessage(content="still no tool"),
        ]
    }
    ctx = InvocationContext(session_id="s", attempt_id="a", stage="evidence", input={})

    with patch(
        "knowcoder_workspace_builder.runtime.agent_tool_call_middleware.active_invocation_context",
        return_value=ctx,
    ):
        assert middleware.after_model(state, SimpleNamespace()) is None
