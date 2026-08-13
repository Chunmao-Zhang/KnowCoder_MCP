"""FastMCP server construction and stdio lifecycle."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from knowcoder_workspace_builder.contracts.integration import server_instructions
from knowcoder_workspace_builder.runtime.session_context import SESSION_ID_ENV

from .tools import MCP_TOOL_HANDLERS


def create_server() -> FastMCP:
    server = FastMCP(
        "KnowCoder Workspace Builder",
        instructions=server_instructions(os.environ.get(SESSION_ID_ENV, "")),
    )
    for handler in MCP_TOOL_HANDLERS:
        # Tool handlers may return either a regular JSON payload or a rich
        # CallToolResult containing a browser link.  FastMCP cannot express
        # that union as structured output, so publish every tool through the
        # protocol's unstructured result envelope.
        server.tool(structured_output=False)(handler)
    return server


def run_stdio() -> None:
    create_server().run(transport="stdio")
