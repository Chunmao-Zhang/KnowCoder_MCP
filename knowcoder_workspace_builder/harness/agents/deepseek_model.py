"""DeepSeek ChatOpenAI wrapper that preserves reasoning_content.

DeepSeek thinking-mode responses can include ``reasoning_content`` on assistant
messages. In multi-turn/tool-call conversations, DeepSeek requires that value to
be sent back with the matching assistant message. langchain-openai currently
drops unknown provider fields during parse/serialization, so this wrapper stores
the field on AIMessage.additional_kwargs and injects it back into later payloads.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI


def _get_mapping_value(value: Any, key: str) -> Any:
    """Read an OpenAI SDK object or dict without depending on SDK internals."""

    if isinstance(value, dict):
        return value.get(key)
    if hasattr(value, key):
        return getattr(value, key)
    extra = getattr(value, "__pydantic_extra__", None) or getattr(value, "model_extra", None) or {}
    if isinstance(extra, dict):
        return extra.get(key)
    return None


def _choice_message(choice: Any) -> Any:
    return _get_mapping_value(choice, "message") or _get_mapping_value(choice, "delta") or {}


def _reasoning_from_choice(choice: Any) -> str | None:
    reasoning = _get_mapping_value(_choice_message(choice), "reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


def _reasoning_from_message(message: AIMessage) -> str | None:
    reasoning = message.additional_kwargs.get("reasoning_content")
    if not reasoning:
        reasoning = message.response_metadata.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


def _flatten_text_blocks(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            return content
        parts.append(block["text"])
    return "".join(parts)


def _supports_parallel_evidence_calls(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    return bool(names.intersection({"web_search", "web_search_batch", "fetch_web_pages"}))


def _canonicalize_tool_calls(api_message: dict[str, Any], message: AIMessage) -> None:
    """Send only tool calls that LangChain parsed and the graph can execute."""
    raw_calls = api_message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return
    valid_ids = {
        str(call.get("id") or "")
        for call in message.tool_calls
        if isinstance(call, dict) and str(call.get("id") or "")
    }
    canonical = [
        call
        for call in raw_calls
        if isinstance(call, dict) and str(call.get("id") or "") in valid_ids
    ]
    if canonical:
        api_message["tool_calls"] = canonical
    else:
        api_message.pop("tool_calls", None)


def _validate_tool_call_pairs(messages: list[dict[str, Any]]) -> None:
    """Fail locally when a request would violate the OpenAI tool-message protocol."""
    pending: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in pending:
                raise ValueError(f"DeepSeek request contains an unmatched tool message: {call_id or '<missing>'}")
            pending.remove(call_id)
            continue
        if pending:
            raise ValueError(
                "DeepSeek request contains tool calls without matching tool messages: "
                + ", ".join(sorted(pending))
            )
        if role != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        pending = {
            str(call.get("id") or "")
            for call in calls
            if isinstance(call, dict) and str(call.get("id") or "")
        }
        if len(pending) != len(calls):
            raise ValueError("DeepSeek request contains a tool call without a unique non-empty id")
    if pending:
        raise ValueError(
            "DeepSeek request contains tool calls without matching tool messages: "
            + ", ".join(sorted(pending))
        )


class DeepSeekChatOpenAI(ChatOpenAI):
    """ChatOpenAI with DeepSeek thinking-mode reasoning preservation."""

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)

        choices = []
        if isinstance(response, dict):
            choices = response.get("choices", []) or []
        elif hasattr(response, "choices"):
            choices = response.choices or []

        for generation, choice in zip(result.generations, choices):
            reasoning = _reasoning_from_choice(choice)
            if reasoning and hasattr(generation.message, "additional_kwargs"):
                generation.message.additional_kwargs["reasoning_content"] = reasoning

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if choices:
            reasoning = _reasoning_from_choice(choices[0])
            message = getattr(generation_chunk, "message", None)
            if reasoning and hasattr(message, "additional_kwargs"):
                existing = message.additional_kwargs.get("reasoning_content", "")
                message.additional_kwargs["reasoning_content"] = f"{existing}{reasoning}"

        return generation_chunk

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        if payload.get("tools"):
            payload["parallel_tool_calls"] = _supports_parallel_evidence_calls(payload["tools"])

        for message in payload.get("messages", []):
            message["content"] = _flatten_text_blocks(message.get("content"))

        langchain_messages = self._convert_input(input_).to_messages()
        assistant_messages = [message for message in langchain_messages if isinstance(message, AIMessage)]
        api_assistant_messages = [
            message for message in payload.get("messages", []) if message.get("role") == "assistant"
        ]
        if len(api_assistant_messages) != len(assistant_messages):
            raise ValueError(
                "DeepSeek request conversion changed the assistant message count: "
                f"input={len(assistant_messages)}, payload={len(api_assistant_messages)}"
            )
        for api_message, langchain_message in zip(api_assistant_messages, assistant_messages, strict=True):
            _canonicalize_tool_calls(api_message, langchain_message)
            reasoning = _reasoning_from_message(langchain_message)
            if reasoning:
                api_message["reasoning_content"] = reasoning

        _validate_tool_call_pairs(payload.get("messages", []))

        return payload
