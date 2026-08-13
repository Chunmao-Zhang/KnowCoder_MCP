"""Tool Filter Middleware

根据 harness.json 中 agent 配置的 tools.allow/deny 过滤所有工具（包括 DeepAgents 内置工具）。

工作原理：
- 在 wrap_model_call 阶段，从发给模型的 tools 列表中过滤掉不允许的工具
- 模型看不到被过滤的工具，自然不会调用它们

过滤规则：
- allow: ["*"] 表示全部允许（只看 deny）
- allow: ["read_file", "web_search"] 表示只允许这些工具
- deny 优先于 allow：即使在 allow 中，deny 里的工具也会被移除
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
    ToolCallRequest,
)
from langchain_core.messages import ToolMessage
from langgraph.types import Command


def _tool_name(tool: Any) -> str | None:
    """从 BaseTool 或 dict 中提取工具名"""
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


class ToolFilterMiddleware(AgentMiddleware):
    """根据 allow/deny 规则过滤模型可见的工具列表

    Args:
        allow: 允许的工具名列表，["*"] 表示全部允许
        deny: 禁止的工具名列表，优先于 allow
    """

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None):
        self._allow = {"*"} if allow is None else set(allow)
        self._deny = set(deny) if deny else set()

    def _should_include(self, name: str | None) -> bool:
        """判断工具是否应该保留"""
        if name is None:
            return True  # 无法识别名称的工具保留
        if name in self._deny:
            return False
        if "*" in self._allow:
            return True
        return name in self._allow

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """在发给模型前过滤工具列表"""
        filtered = [t for t in request.tools if self._should_include(_tool_name(t))]
        return handler(request.override(tools=filtered))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable,
    ) -> ModelResponse[ResponseT]:
        """异步版本"""
        filtered = [t for t in request.tools if self._should_include(_tool_name(t))]
        return await handler(request.override(tools=filtered))


class ToolExecutionFilterMiddleware(AgentMiddleware):
    """Block disallowed tool execution even if a tool appears through framework defaults."""

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None):
        self._allow = {"*"} if allow is None else set(allow)
        self._deny = set(deny) if deny else set()

    def _should_include(self, name: str | None) -> bool:
        if name is None:
            return True
        if name in self._deny:
            return False
        if "*" in self._allow:
            return True
        return name in self._allow

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        name = request.tool_call.get("name")
        if not self._should_include(name):
            return ToolMessage(
                content=f"Tool '{name}' is not allowed for this agent. Use the configured allowed tools only.",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        name = request.tool_call.get("name")
        if not self._should_include(name):
            return ToolMessage(
                content=f"Tool '{name}' is not allowed for this agent. Use the configured allowed tools only.",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return await handler(request)
