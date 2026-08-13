"""SFT 记录器

将 agent 执行的消息流转换为 OpenAI messages 格式并落盘为 JSONL。
每次 run 生成一条完整样本，写入 runs/harness_conversation_logs/<date>/<run_id>/messages.jsonl。

OpenAI messages 格式：
[
  {"role": "system", "content": "..."},
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "...", "tool_calls": [...]},
  {"role": "tool", "tool_call_id": "...", "content": "..."},
  {"role": "assistant", "content": "..."}
]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def convert_message(msg: Any) -> dict[str, Any] | None:
    """将单条 LangChain message 转换为 OpenAI 格式

    Returns:
        OpenAI 格式的 dict，或 None（跳过无意义消息）
    """
    msg_type = getattr(msg, "type", None)
    content = getattr(msg, "content", "") or ""

    if msg_type in ("human", "user"):
        return {"role": "user", "content": content}

    elif msg_type in ("ai", "assistant"):
        out: dict[str, Any] = {"role": "assistant", "content": content}
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        # 跳过空 assistant 消息（无 content 也无 tool_calls）
        if not content and not tool_calls:
            return None
        return out

    elif msg_type == "tool":
        tool_call_id = getattr(msg, "tool_call_id", "") or ""
        name = getattr(msg, "name", "") or ""
        out = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        }
        if name:
            out["name"] = name
        return out

    elif msg_type == "system":
        return {"role": "system", "content": content}

    # 未知类型，尽力转换
    return {"role": msg_type or "unknown", "content": content}


def convert_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """将完整的 LangChain messages 列表转换为 OpenAI 格式"""
    result = []
    for msg in messages:
        converted = convert_message(msg)
        if converted is not None:
            result.append(converted)
    return result


def record_run(
    messages: list[Any],
    output_path: str | Path,
    metadata: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> Path:
    """将一次 run 的消息记录为 JSONL 格式

    每行是一个完整的训练样本：
    {"messages": [...], "metadata": {...}}

    Args:
        messages: LangChain messages 列表（agent invoke 的结果）
        output_path: 输出文件路径
        metadata: 附加元数据（run_id, agent_id 等）
        system_prompt: system prompt 内容，非空时插入到消息列表最前面

    Returns:
        写入的文件路径
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    openai_messages = convert_messages(messages)

    # 如果提供了 system_prompt，插入到最前面（去重：如果已有 system 消息则不重复插入）
    if system_prompt:
        has_system = any(m.get("role") == "system" for m in openai_messages)
        if not has_system:
            openai_messages.insert(0, {"role": "system", "content": system_prompt})

    sample = {"messages": openai_messages}
    if metadata:
        sample["metadata"] = metadata

    with open(output_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    return output_path
