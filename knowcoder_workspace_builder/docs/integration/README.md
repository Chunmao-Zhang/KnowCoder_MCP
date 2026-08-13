# Host Integration

The stdio command is `python -m knowcoder_workspace_builder.mcp_server`. Set its
working directory and `SCHEMA_WORKSPACE_PROJECT` to the same selected project.

For Codex, configure `mcp_servers.knowcoder_workspace_builder` with the local Python
executable, `-m knowcoder_workspace_builder.mcp_server`, the selected project as
`cwd`, and the same project in `SCHEMA_WORKSPACE_PROJECT`. Supply model and web
credentials through the host environment, outside source control.

The host follows each returned `next_action`, shows both confirmation gates, and
reads only the final four public Workspace files. For completed-task changes, it
selects the smallest semantic impact declared by `resume_workspace_build`; the
decision is based on meaning rather than keywords.
