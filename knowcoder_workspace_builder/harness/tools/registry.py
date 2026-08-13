"""工具注册表

职责：
- 根据 agent workspace 动态加载工具（workspace 自动扫描）
- 提供少量 harness 内置基础工具
- 根据 agent 配置的 tools.allow/deny 过滤
- 返回该 agent 可用的工具列表
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool

from knowcoder_workspace_builder.harness.config.schema import AgentConfig
from knowcoder_workspace_builder.harness.tools.code_executor import execute_code
from knowcoder_workspace_builder.harness.tools.file_writer import write_file
from knowcoder_workspace_builder.harness.tools.web_search import web_search
from knowcoder_workspace_builder.harness.tools.workspace_loader import load_workspace_tools


def _harden_tool(tool: BaseTool) -> BaseTool:
    """Wrap a tool so an unexpected exception is returned as an error result
    instead of propagating and aborting the entire agent run.

    LangGraph's ToolNode re-raises any non-``ToolException`` a tool raises, which
    tears down the whole graph. Converting failures into a JSON error string lets
    the model see what went wrong and recover (retry, fix arguments, move on).
    """
    func = getattr(tool, "func", None)
    coroutine = getattr(tool, "coroutine", None)
    updates: dict[str, object] = {}

    if callable(func):
        def safe_func(*args, __func=func, __name=tool.name, **kwargs):
            try:
                return __func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced to the model.
                return json.dumps(
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": f"{__name} failed: {exc}",
                    },
                    ensure_ascii=False,
                )

        updates["func"] = safe_func

    if callable(coroutine):
        async def safe_coroutine(*args, __coro=coroutine, __name=tool.name, **kwargs):
            try:
                return await __coro(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced to the model.
                return json.dumps(
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": f"{__name} failed: {exc}",
                    },
                    ensure_ascii=False,
                )

        updates["coroutine"] = safe_coroutine

    if not updates:
        return tool
    try:
        return tool.model_copy(update=updates)
    except Exception:  # noqa: BLE001 - fall back to in-place wrapping.
        for key, value in updates.items():
            setattr(tool, key, value)
        return tool


# harness 内置基础工具，所有 agent 都可以按需 allow/deny
_BASE_TOOLS: dict[str, BaseTool] = {
    "execute_code": execute_code,
    "write_file": write_file,
    "web_search": web_search,
}

# 保留向后兼容导出，值等同于 _BASE_TOOLS（不再包含 KBQA 工具）
ALL_CUSTOM_TOOLS: dict[str, BaseTool] = dict(_BASE_TOOLS)


def _catalog_for_agent(
    agent_cfg: AgentConfig,
    workspace_dir: str | None = None,
    harness_root: str | None = None,
) -> dict[str, BaseTool]:
    """Return the full tool catalog for an agent.

    工具来源优先级（低 -> 高，高者覆盖同名工具）：
      1. harness 内置基础工具
      2. workspace-local 工具（从 workspace/tools/*.py 扫描）

    mode=replace 时只有基础工具 + workspace 工具；
    mode=extend  时继承基础工具 + workspace 工具（目前基础工具即全部全局工具）。
    """
    workspace_spec = load_workspace_tools(workspace_dir, harness_root)
    workspace_tools = {tool.name: tool for tool in workspace_spec.tools}
    # 两种模式结果相同（基础工具 + workspace 工具），replace 语义已无差别，
    # 保留字段是为了让 workspace 未来可以显式声明"完全替换"。
    return {
        **_BASE_TOOLS,
        **workspace_tools,
    }


def get_tools_for_agent(
    agent_cfg: AgentConfig,
    workspace_dir: str | None = None,
    harness_root: str | None = None,
) -> list[BaseTool]:
    """根据 agent 配置的 allow/deny 返回可用工具列表

    规则：
    - allow: ["*"] 表示所有工具可用
    - deny 优先于 allow
    """
    catalog = _catalog_for_agent(agent_cfg, workspace_dir=workspace_dir, harness_root=harness_root)
    tools_cfg = agent_cfg.tools
    if tools_cfg is None:
        return list(catalog.values())

    allow = tools_cfg.allow
    deny = set(tools_cfg.deny)

    # 确定候选工具；保留 catalog 插入顺序，避免工具列表随机抖动。
    if "*" in allow:
        candidates = list(catalog.keys())
    else:
        allow_set = set(allow)
        candidates = [name for name in catalog.keys() if name in allow_set]

    final = [name for name in candidates if name not in deny]
    return [_harden_tool(catalog[name]) for name in final if name in catalog]
