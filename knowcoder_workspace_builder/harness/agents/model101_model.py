"""Model101ChatOpenAI — provider wrapper for Model101's OpenAI-compatible API.

Model101 accepts OpenAI-like chat-completion requests, but we do not want the
harness' graph streaming path to send `stream=true` to this provider. This
wrapper converts the request back to Model101's expected chat-completion shape
and turns LangChain stream calls into a single non-streaming completion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import openai
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


def _flatten_content(content: Any) -> Any:
    """Normalize provider content blocks into plain text when needed."""

    if not isinstance(content, list):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("content") or ""))
        else:
            parts.append(str(item))
    return "".join(parts)


def _normalize_response(response: dict | openai.BaseModel) -> dict | openai.BaseModel:
    """Make small Model101 response variants acceptable to ChatOpenAI parsing."""

    if not isinstance(response, dict):
        return response
    normalized = dict(response)
    choices = normalized.get("choices")
    if not isinstance(choices, list):
        return normalized

    normalized_choices: list[Any] = []
    for choice in choices:
        if not isinstance(choice, dict):
            normalized_choices.append(choice)
            continue
        item = dict(choice)
        if "message" not in item and isinstance(item.get("delta"), dict):
            item["message"] = dict(item["delta"])
        message = item.get("message")
        if isinstance(message, dict):
            message = dict(message)
            message["content"] = _flatten_content(message.get("content"))
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and not isinstance(function.get("arguments", ""), str):
                    function["arguments"] = str(function.get("arguments") or "")
            item["message"] = message
        normalized_choices.append(item)
    normalized["choices"] = normalized_choices
    return normalized


def _extract_reasoning(response: dict | openai.BaseModel) -> list[str | None]:
    choices: list[Any] = []
    if isinstance(response, dict):
        choices = response.get("choices", []) or []
    elif hasattr(response, "choices"):
        choices = getattr(response, "choices") or []

    reasoning_values: list[str | None] = []
    for choice in choices:
        reasoning = None
        if isinstance(choice, dict):
            message = choice.get("message") or choice.get("delta") or {}
            if isinstance(message, dict):
                reasoning = message.get("reasoning_content")
        elif hasattr(choice, "message"):
            message = choice.message
            extra = getattr(message, "__pydantic_extra__", None) or {}
            reasoning = extra.get("reasoning_content") or getattr(message, "reasoning_content", None)
        reasoning_values.append(reasoning)
    return reasoning_values


class Model101ChatOpenAI(ChatOpenAI):
    """ChatOpenAI wrapper that forces non-streaming Model101 requests."""

    def _get_request_payload(
        self,
        input_: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # langchain-openai maps max_tokens -> max_completion_tokens. Model101's
        # OpenAI-compatible chat endpoint expects max_tokens.
        if "max_completion_tokens" in payload and "max_tokens" in payload:
            del payload["max_completion_tokens"]
        elif "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")

        # The wrapper handles stream requests by making a normal completion and
        # returning one chunk to LangChain, so the provider request must not stream.
        if payload.get("stream"):
            payload["stream"] = False

        return payload

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(_normalize_response(response), generation_info)
        for generation, reasoning in zip(result.generations, _extract_reasoning(response)):
            if reasoning and hasattr(generation.message, "additional_kwargs"):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _message_to_generation_chunk(
        self,
        message: BaseMessage,
        generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk:
        if isinstance(message, AIMessage):
            chunk = AIMessageChunk(
                content=message.content,
                additional_kwargs=dict(message.additional_kwargs),
                response_metadata=dict(message.response_metadata),
                id=message.id,
                name=message.name,
                tool_calls=list(message.tool_calls or []),
                invalid_tool_calls=list(message.invalid_tool_calls or []),
                usage_metadata=message.usage_metadata,
                chunk_position="last",
            )
        else:
            chunk = AIMessageChunk(
                content=str(message.content or ""),
                additional_kwargs=dict(getattr(message, "additional_kwargs", {}) or {}),
                response_metadata=dict(getattr(message, "response_metadata", {}) or {}),
                id=getattr(message, "id", None),
                name=getattr(message, "name", None),
                chunk_position="last",
            )
        return ChatGenerationChunk(message=chunk, generation_info=generation_info)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        kwargs = dict(kwargs)
        kwargs.pop("stream", None)
        result = self._generate(messages, stop=stop, run_manager=None, stream=False, **kwargs)
        for generation in result.generations:
            chunk = self._message_to_generation_chunk(generation.message, generation.generation_info)
            if run_manager:
                run_manager.on_llm_new_token(
                    chunk.text,
                    chunk=chunk,
                    logprobs=(generation.generation_info or {}).get("logprobs"),
                )
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        kwargs = dict(kwargs)
        kwargs.pop("stream", None)
        result = await self._agenerate(messages, stop=stop, run_manager=None, stream=False, **kwargs)
        for generation in result.generations:
            chunk = self._message_to_generation_chunk(generation.message, generation.generation_info)
            if run_manager:
                await run_manager.on_llm_new_token(
                    chunk.text,
                    chunk=chunk,
                    logprobs=(generation.generation_info or {}).get("logprobs"),
                )
            yield chunk
