"""FridayChatOpenAI — ChatOpenAI subclass that preserves reasoning_content.

Some OpenAI-compatible thinking models return `reasoning_content` in
choices[].message or streamed deltas, but:
1. langchain-openai's `_create_chat_result` doesn't parse it into the AIMessage
2. langchain-openai's `_convert_message_to_dict` doesn't serialize it back

Both must be fixed for multi-turn tool-call conversations to work against APIs
that require the thinking trace to be echoed back with assistant tool calls.

Fix:
- Override `_create_chat_result` to extract and store reasoning_content in
  AIMessage.additional_kwargs
- Override `_get_request_payload` to inject reasoning_content back into the
  messages array before sending to the API
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI


def _get_mapping_value(value: Any, key: str) -> Any:
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


def _canonicalize_tool_calls(api_message: dict[str, Any], message: AIMessage) -> None:
    """Keep only parsed tool calls that the agent graph can execute."""
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
    pending: set[str] = set()
    for message in messages:
        role = str(message.get("role") or "")
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if not call_id or call_id not in pending:
                raise ValueError(f"Friday request contains an unmatched tool message: {call_id or '<missing>'}")
            pending.remove(call_id)
            continue
        if pending:
            raise ValueError(
                "Friday request contains tool calls without matching tool messages: "
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
            raise ValueError("Friday request contains a tool call without a unique non-empty id")
    if pending:
        raise ValueError(
            "Friday request contains tool calls without matching tool messages: "
            + ", ".join(sorted(pending))
        )


class FridayChatOpenAI(ChatOpenAI):
    """ChatOpenAI with reasoning_content preservation for thinking models."""

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)

        choices: list[Any] = []
        if isinstance(response, dict):
            choices = response.get("choices", []) or []
        elif hasattr(response, "choices") and response.choices:
            choices = list(response.choices)

        for gen, choice in zip(result.generations, choices):
            reasoning = _reasoning_from_choice(choice)
            if reasoning and hasattr(gen.message, "additional_kwargs"):
                gen.message.additional_kwargs["reasoning_content"] = reasoning

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

        # Fix: langchain-openai >= 0.3 renames max_tokens -> max_completion_tokens,
        # but Friday's API proxy does NOT accept max_completion_tokens and rejects
        # requests when both params appear simultaneously.
        # Solution: always keep only max_tokens, drop max_completion_tokens.
        if "max_completion_tokens" in payload and "max_tokens" in payload:
            del payload["max_completion_tokens"]
        elif "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")

        for message in payload.get("messages", []):
            message["content"] = _flatten_text_blocks(message.get("content"))

        # Match converted request messages to the original LangChain messages by
        # assistant order. System prompt injection can shift raw message indexes.
        langchain_messages = self._convert_input(input_).to_messages()
        assistant_messages = [message for message in langchain_messages if isinstance(message, AIMessage)]
        api_assistant_messages = [
            message for message in payload.get("messages", []) if message.get("role") == "assistant"
        ]
        if len(api_assistant_messages) != len(assistant_messages):
            raise ValueError(
                "Friday request conversion changed the assistant message count: "
                f"input={len(assistant_messages)}, payload={len(api_assistant_messages)}"
            )
        for api_message, langchain_message in zip(api_assistant_messages, assistant_messages, strict=True):
            _canonicalize_tool_calls(api_message, langchain_message)
            reasoning = _reasoning_from_message(langchain_message)
            if reasoning:
                api_message["reasoning_content"] = reasoning

        _validate_tool_call_pairs(payload.get("messages", []))

        return payload
