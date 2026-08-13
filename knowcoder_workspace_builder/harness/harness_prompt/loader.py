"""Harness Prompt 加载器

加载 harness_prompt/ 目录下的通用 prompt 文件，
按固定顺序拼装，支持 {agent_id} 变量替换。

加载顺序：
1. tools_prompt.md  — 工具说明（每个工具是什么、怎么调用）
2. workflow_prompt.md — 工作流规范（多工具协作的流程）
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_DIR = Path(__file__).parent

# 按顺序加载的 prompt 文件
_PROMPT_FILES = [
    "tools_prompt.md",
    "workflow_prompt.md",
]


def load_harness_prompt(agent_id: str) -> str:
    """加载并拼装通用 harness prompt

    Args:
        agent_id: 当前 agent 的 ID，用于替换 {agent_id}

    Returns:
        拼装好的通用 prompt 字符串
    """
    parts = []
    for filename in _PROMPT_FILES:
        path = _PROMPT_DIR / filename
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            content = content.replace("{agent_id}", agent_id)
            parts.append(content)

    return "\n\n".join(parts)
