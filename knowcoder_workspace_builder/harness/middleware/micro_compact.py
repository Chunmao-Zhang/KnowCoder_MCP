"""Micro-Compact Middleware

参考 Claude Code 的微压缩策略：
- 每次工具调用后，将结果落盘到 runs/harness_conversation_logs/<date>/<run_id>/tool_results/<tool_call_id>.txt
- 保留最近 N 轮的工具结果不动
- N 轮之前的指定工具的返回结果替换为引用占位符
- task、compact_conversation 等工具的结果永远不压缩

被压缩的工具列表（产出大量 token 的工具）：
- read_file, write_file, edit_file
- ls, glob, grep
- execute, execute_code
- web_search

不压缩的工具（结果本身就是摘要或状态信息）：
- task（子 agent 返回的摘要）
- compact_conversation
- write_todos
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AnyMessage, ToolMessage
from langgraph.types import Command

# 默认保留最近 N 轮消息不压缩
DEFAULT_KEEP_TURNS = 3

# 需要被压缩的工具名称（产出大量 token 的工具）
DEFAULT_COMPACT_TOOLS = frozenset([
    "read_file",
    "write_file",
    "edit_file",
    "ls",
    "glob",
    "grep",
    "execute",
    "web_search",
    "web_search_batch",
    "fetch_web_pages",
])


def _get_run_dir() -> Path | None:
    """从环境变量获取当前 run 的目录"""
    run_dir = os.environ.get("HARNESS_RUN_DIR")
    if run_dir:
        return Path(run_dir)
    return None


def _tool_results_dir(run_dir: Path) -> Path:
    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == run_dir.expanduser().resolve():
        return run_dir / "intermediate" / "tool_results"
    return run_dir / "tool_results"


def _save_tool_result(tool_call_id: str, tool_name: str, content: str) -> str | None:
    """将工具结果落盘到 run 目录，返回保存的虚拟路径（供 read_file 使用）"""
    run_dir = _get_run_dir()
    if not run_dir:
        return None

    results_dir = _tool_results_dir(run_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{tool_call_id}.txt"
    file_path = results_dir / filename
    file_path.write_text(content, encoding="utf-8")

    session_root = str(os.environ.get("SCHEMA_WORKSPACE_SESSION_ROOT") or "").strip()
    if session_root and Path(session_root).expanduser().resolve() == run_dir.expanduser().resolve():
        relative = file_path.resolve().relative_to(run_dir.resolve())
        return f"/.knowcoder_workspace/{relative.as_posix()}"

    # 返回虚拟路径（相对于 harness_root，以 / 开头，供 read_file 使用）
    harness_root = os.environ.get("HARNESS_ROOT", "")
    if harness_root:
        try:
            rel = file_path.resolve().relative_to(Path(harness_root).resolve())
            return f"/{rel}"
        except ValueError:
            pass
    return str(file_path)


def _count_turns(messages: list[AnyMessage]) -> list[tuple[int, int]]:
    """将消息列表按 turn 分组，返回每个 turn 的 (start_idx, end_idx)"""
    turns: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        start = i
        msg_type = getattr(messages[i], "type", "")
        i += 1
        if msg_type == "ai":
            while i < len(messages) and getattr(messages[i], "type", "") == "tool":
                i += 1
        turns.append((start, i))
    return turns


def _compact_messages(
    messages: list[AnyMessage],
    keep_turns: int,
    compact_tools: frozenset[str],
    saved_paths: dict[str, str],
) -> list[AnyMessage]:
    """对消息列表执行微压缩

    Args:
        messages: 原始消息列表
        keep_turns: 保留最近多少轮不压缩
        compact_tools: 需要压缩的工具名称集合
        saved_paths: tool_call_id -> 落盘文件虚拟路径 的映射
    """
    turns = _count_turns(messages)

    if len(turns) <= keep_turns:
        return messages

    compact_end_idx = turns[-keep_turns][0] if keep_turns > 0 else len(messages)

    result = []
    for i, msg in enumerate(messages):
        if i < compact_end_idx and getattr(msg, "type", "") == "tool":
            tool_name = getattr(msg, "name", "") or ""
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            if tool_name in compact_tools:
                # 构造引用占位符
                saved_path = saved_paths.get(tool_call_id)
                if saved_path:
                    placeholder = f"[内容已压缩 - 使用 read_file 查看: {saved_path}]"
                else:
                    placeholder = "[内容已压缩]"

                compacted = ToolMessage(
                    content=placeholder,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
                result.append(compacted)
                continue
        result.append(msg)

    return result


class MicroCompactMiddleware(AgentMiddleware):
    """微压缩 Middleware

    两个职责：
    1. wrap_tool_call: 每次工具调用后，将结果落盘到 run 目录
    2. wrap_model_call: 发给模型前，将旧的工具结果替换为引用占位符

    Args:
        keep_turns: 保留最近多少轮不压缩（默认 3）
        compact_tools: 需要压缩的工具名称集合
    """

    def __init__(
        self,
        keep_turns: int = DEFAULT_KEEP_TURNS,
        compact_tools: frozenset[str] | set[str] | None = None,
    ):
        self._keep_turns = keep_turns
        self._compact_tools = frozenset(compact_tools) if compact_tools else DEFAULT_COMPACT_TOOLS
        # tool_call_id -> 落盘文件虚拟路径
        self._saved_paths: dict[str, str] = {}

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """拦截工具调用结果，落盘到 run 目录"""
        result = handler(request)

        # 只处理 ToolMessage（Command 不处理）
        if isinstance(result, ToolMessage):
            tool_call_id = getattr(result, "tool_call_id", "") or ""
            tool_name = getattr(result, "name", "") or ""

            # 只落盘需要压缩的工具的结果
            if tool_name in self._compact_tools and tool_call_id:
                content = getattr(result, "content", "") or ""
                saved_path = _save_tool_result(tool_call_id, tool_name, content)
                if saved_path:
                    self._saved_paths[tool_call_id] = saved_path

        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable,
    ) -> ToolMessage | Command:
        """异步版本"""
        result = await handler(request)

        if isinstance(result, ToolMessage):
            tool_call_id = getattr(result, "tool_call_id", "") or ""
            tool_name = getattr(result, "name", "") or ""

            if tool_name in self._compact_tools and tool_call_id:
                content = getattr(result, "content", "") or ""
                saved_path = _save_tool_result(tool_call_id, tool_name, content)
                if saved_path:
                    self._saved_paths[tool_call_id] = saved_path

        return result

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """在发给模型之前，对消息做微压缩"""
        compacted = _compact_messages(
            request.messages,
            self._keep_turns,
            self._compact_tools,
            self._saved_paths,
        )
        return handler(request.override(messages=compacted))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable,
    ) -> ModelResponse[ResponseT]:
        """异步版本"""
        compacted = _compact_messages(
            request.messages,
            self._keep_turns,
            self._compact_tools,
            self._saved_paths,
        )
        return await handler(request.override(messages=compacted))
