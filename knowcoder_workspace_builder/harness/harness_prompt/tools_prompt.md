# Tool Guide

## Role

Use the tools enabled for the current agent.

## Objective

Complete tool-based work with verified results.

## Inputs

Use the current tool catalog and workspace instructions.

## Workflow

1. Select the required tool.
2. Pass complete arguments.
3. Inspect the returned status and output.
4. Repair explicit errors.
5. Continue until the task is complete.

## Output Contract

Base every conclusion on verified tool output.

## Rules

- Follow workspace path rules first.
- Use virtual `/workspaces/` paths when no workspace rule exists.
- Set finite read limits.
- Set explicit timeouts for long commands.
- Keep secrets out of tool arguments and output.

## Tools

- `web_search`: search the web.
- `execute`: run a shell command.

## Examples

### Execute A Script

```text
execute(command="python3 /workspaces/{agent_id}/code/analyze.py")
```
