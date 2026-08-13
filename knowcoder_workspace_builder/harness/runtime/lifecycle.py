"""Run 生命周期管理

负责：
- 创建 run 目录和 meta.json
- 更新状态（running -> completed / failed）
- 记录步骤数和耗时
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from knowcoder_workspace_builder.harness.runtime.context import RuntimeContext


class RunLifecycle:
    """管理单次 run 的生命周期"""

    def __init__(self, ctx: RuntimeContext):
        self.ctx = ctx

    def start(self) -> None:
        """开始 run：创建目录 + 写 meta.json"""
        self.ctx.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_meta({
            "run_id": self.ctx.run_id,
            "agent_id": self.ctx.agent_id,
            "parent_run_id": self.ctx.parent_run_id,
            "status": "running",
            "started_at": self.ctx.started_at,
            "finished_at": None,
            "steps": 0,
            "error": None,
        })

    def finish(self, error: str | None = None) -> None:
        """结束 run：更新 meta.json 状态"""
        meta = self._read_meta()
        meta["status"] = "failed" if error else "completed"
        meta["finished_at"] = datetime.now().isoformat()
        meta["steps"] = self.ctx.current_step
        meta["error"] = error
        self._write_meta(meta)

    def update_step(self) -> None:
        """更新当前步骤数到 meta.json"""
        meta = self._read_meta()
        meta["steps"] = self.ctx.current_step
        self._write_meta(meta)

    def _meta_path(self):
        return self.ctx.run_dir / "meta.json"

    def _write_meta(self, meta: dict[str, Any]) -> None:
        with open(self._meta_path(), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _read_meta(self) -> dict[str, Any]:
        path = self._meta_path()
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
