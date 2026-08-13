"""RuntimeContext：单次 run 的全局状态容器

透传给所有工具、middleware、子 agent。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class RuntimeContext:
    """单次运行的全局上下文"""

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    agent_id: str = ""
    parent_run_id: str | None = None
    workspace_dir: str = ""
    harness_root: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    current_step: int = 0
    max_steps: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def date_str(self) -> str:
        return self.started_at[:10]

    @property
    def run_dir(self) -> Path:
        """本次 harness trace 输出目录: runs/harness_conversation_logs/<date>/<run_id>/"""
        return Path(self.harness_root) / "runs" / "harness" / self.date_str / self.run_id

    def step(self) -> int:
        """步骤 +1，返回当前步骤号"""
        self.current_step += 1
        return self.current_step

    def is_over_limit(self) -> bool:
        if self.max_steps is None or self.max_steps <= 0:
            return False
        return self.current_step >= self.max_steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "parent_run_id": self.parent_run_id,
            "workspace_dir": self.workspace_dir,
            "harness_root": self.harness_root,
            "started_at": self.started_at,
            "current_step": self.current_step,
            "max_steps": self.max_steps,
            "extra": self.extra,
        }
