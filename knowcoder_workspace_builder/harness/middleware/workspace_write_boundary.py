"""Global Harness middleware for the selected project's write boundary."""

from __future__ import annotations

import json
from collections.abc import Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from knowcoder_workspace_builder.harness.write_boundary import (
    WriteBoundaryError,
    require_active_run_root,
    require_workspace_write_path,
    workspace_write_root,
)


class WorkspaceWriteBoundaryMiddleware(AgentMiddleware):
    """Allow Harness writes only below the selected `.knowcoder_workspace`."""

    _PATH_WRITE_TOOLS = {"write_file", "edit_file", "execute_code"}
    _RUN_SCOPED_WRITE_TOOLS = {
        "append_instances_batch",
        "append_instances_batches_from_file",
        "save_evidence_manifest",
        "save_schema",
        "save_upload_classification",
        "web_search",
        "web_search_batch",
        "fetch_web_pages",
    }

    def __init__(self) -> None:
        self.write_root = workspace_write_root()

    @staticmethod
    def _blocked(request: ToolCallRequest, reason: str) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(
                {
                    "ok": False,
                    "error_type": "workspace_write_boundary",
                    "error": reason,
                },
                ensure_ascii=False,
            ),
            tool_call_id=request.tool_call["id"],
            status="error",
        )

    def _check(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        args = request.tool_call.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            if tool_name == "execute":
                raise WriteBoundaryError(
                    "shell execution is disabled because its filesystem writes cannot be bounded portably"
                )
            if tool_name in self._PATH_WRITE_TOOLS:
                file_path = str(args.get("file_path") or args.get("path") or "")
                require_workspace_write_path(file_path)
            if tool_name in self._RUN_SCOPED_WRITE_TOOLS:
                require_active_run_root()
        except WriteBoundaryError as exc:
            return self._blocked(request, str(exc))
        return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        error = self._check(request)
        if error is not None:
            return error
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        error = self._check(request)
        if error is not None:
            return error
        return await handler(request)
