"""Stable stdio MCP entrypoint."""

from knowcoder_workspace_builder.mcp.server import run_stdio


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
