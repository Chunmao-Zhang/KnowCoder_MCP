"""Load MCP server tools as LangChain tools.

This module is intentionally generic. It does not know about ontology or any
specific MCP server. It reads MCP connection config and exposes the server's
tools to the harness agent loop as normal LangChain StructuredTool instances.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import BaseModel, ConfigDict, Field, create_model

from knowcoder_workspace_builder.harness.config.schema import MCPServerConfig
from knowcoder_workspace_builder.runtime.timeouts import mcp_tool_timeout_seconds

_SERVER_INSTRUCTIONS: dict[str, str] = {}


def loaded_mcp_server_instructions() -> list[str]:
    """Return instructions reported by enabled MCP servers initialized in this process."""
    return [value for value in _SERVER_INSTRUCTIONS.values() if value.strip()]


def load_mcp_tools(
    servers: dict[str, MCPServerConfig] | None,
    *,
    harness_root: str | Path,
) -> list[BaseTool]:
    """Load tools from configured MCP servers."""
    if not servers:
        return []
    tools: list[BaseTool] = []
    for server_name, server_cfg in servers.items():
        if not server_cfg.enabled:
            continue
        if server_cfg.transport != "stdio":
            # Keep the first implementation small and deterministic. Additional
            # transports can be added here without changing agent code.
            continue
        if not server_cfg.command:
            continue
        try:
            specs = _run_async(_list_tools(server_cfg, harness_root=harness_root))
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize enabled MCP server {server_name!r}: {exc}") from exc
        _SERVER_INSTRUCTIONS[server_name] = next(
            (str(spec.get("_serverInstructions") or "").strip() for spec in specs if spec.get("_serverInstructions")),
            "",
        )
        for spec in specs:
            tool_name = str(spec.get("name", "") or "")
            if not tool_name:
                continue
            args_schema = _args_schema_from_json_schema(
                f"MCP_{server_name}_{tool_name}_Input",
                spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {},
            )
            server_instructions = str(spec.get("_serverInstructions") or "").strip()
            tool_description = str(spec.get("description") or f"MCP tool `{tool_name}` from `{server_name}`.").strip()
            if server_instructions:
                tool_description = f"{server_instructions}\n\n{tool_description}"
            tools.append(
                StructuredTool.from_function(
                    func=_make_sync_tool(server_cfg, tool_name, harness_root=harness_root),
                    coroutine=_make_async_tool(server_cfg, tool_name, harness_root=harness_root),
                    name=tool_name,
                    description=tool_description,
                    args_schema=args_schema,
                )
            )
    return tools


async def _list_tools(server_cfg: MCPServerConfig, *, harness_root: str | Path) -> list[dict[str, Any]]:
    params = _stdio_params(server_cfg, harness_root=harness_root)
    async with stdio_client(params) as (read, write):
        timeout_seconds = mcp_tool_timeout_seconds()
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        ) as session:
            initialization = await session.initialize()
            result = await session.list_tools()
            instructions = str(getattr(initialization, "instructions", "") or "").strip()
            specs = [tool.model_dump(by_alias=True) for tool in result.tools]
            for spec in specs:
                spec["_serverInstructions"] = instructions
            return specs


def _make_sync_tool(server_cfg: MCPServerConfig, tool_name: str, *, harness_root: str | Path):
    def _call(**kwargs: Any) -> str:
        return _run_async(_call_tool(server_cfg, tool_name, kwargs, harness_root=harness_root))

    return _call


def _make_async_tool(server_cfg: MCPServerConfig, tool_name: str, *, harness_root: str | Path):
    async def _acall(**kwargs: Any) -> str:
        return await _call_tool(server_cfg, tool_name, kwargs, harness_root=harness_root)

    return _acall


async def _call_tool(
    server_cfg: MCPServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    harness_root: str | Path,
) -> str:
    params = _stdio_params(server_cfg, harness_root=harness_root)
    async with stdio_client(params) as (read, write):
        timeout_seconds = mcp_tool_timeout_seconds()
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        ) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.structuredContent is not None:
                return json.dumps(result.structuredContent, ensure_ascii=False)
            parts: list[str] = []
            for item in result.content:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                else:
                    try:
                        parts.append(item.model_dump_json(by_alias=True))
                    except Exception:
                        parts.append(str(item))
            if result.isError:
                return json.dumps({"ok": False, "error": "\n".join(parts)}, ensure_ascii=False)
            return "\n".join(parts)


def _stdio_params(server_cfg: MCPServerConfig, *, harness_root: str | Path) -> StdioServerParameters:
    root = Path(harness_root).resolve()
    target_root = Path(os.environ.get("KNOWCODER_TARGET_PROJECT_ROOT") or os.getcwd()).resolve()
    cwd = server_cfg.cwd or str(root)
    cwd = _expand_runtime_tokens(cwd, root=root, target_root=target_root)
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        cwd_path = root / cwd_path
    env = dict(os.environ)
    for key, value in server_cfg.env.items():
        env[key] = _expand_runtime_tokens(str(value), root=root, target_root=target_root)
    env.setdefault("HARNESS_ROOT", str(root))
    env.setdefault("KNOWCODER_TARGET_PROJECT_ROOT", str(target_root))
    return StdioServerParameters(
        command=_expand_runtime_tokens(server_cfg.command, root=root, target_root=target_root),
        args=server_cfg.args,
        cwd=cwd_path,
        env=env,
    )


def _expand_runtime_tokens(value: str, *, root: Path, target_root: Path) -> str:
    return (
        value.replace("{root}", str(root))
        .replace("{root_parent}", str(root.parent))
        .replace("{target_root}", str(target_root))
        .replace("{python}", sys.executable)
    )


def _args_schema_from_json_schema(model_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") if isinstance(schema.get("required"), list) else [])
    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        if not isinstance(name, str) or not isinstance(prop, dict):
            continue
        typ = _python_type_for_json_schema(prop)
        default = ... if name in required else _default_for_json_schema(prop)
        description = str(prop.get("description") or "")
        if description:
            fields[name] = (typ, Field(default, description=description))
        else:
            fields[name] = (typ, default)
    return create_model(
        model_name,
        __base__=_MCPArgsBase,
        **fields,
    )


class _MCPArgsBase(BaseModel):
    model_config = ConfigDict(extra="allow")


def _python_type_for_json_schema(prop: dict[str, Any]) -> Any:
    json_type = prop.get("type")
    if isinstance(json_type, list):
        json_type = next((item for item in json_type if item != "null"), "string")
    if json_type == "boolean":
        return bool
    if json_type == "integer":
        return int
    if json_type == "number":
        return float
    if json_type == "array":
        return list[Any]
    if json_type == "object":
        return dict[str, Any]
    return str


def _default_for_json_schema(prop: dict[str, Any]) -> Any:
    if "default" in prop:
        return prop.get("default")
    json_type = prop.get("type")
    if isinstance(json_type, list) and "null" in json_type:
        return None
    return None


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Synchronous LangChain tool calls normally do not happen inside an event
    # loop. If they do, run the coroutine in a short-lived thread with its own
    # event loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(coro)).result()
