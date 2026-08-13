# Workflow Guide

## Role

Guide tool-based code execution.

## Objective

Run one verifiable script from the correct workspace path.

## Inputs

Use current workspace instructions and tool results.

## Workflow

1. Choose the required output path.
2. Write a meaningful script name.
3. Write the script.
4. Execute the script.
5. Inspect stdout, stderr, and exit status.
6. Continue from the verified result.

## Output Contract

Return conclusions supported by the execution result.

## Rules

- Follow workspace path rules first.
- Use `/workspaces/{agent_id}/code/` only when no path is supplied.
- Read compressed tool output from its returned file path.
- Keep generated filenames meaningful.

## Tools

- `write_file`
- `execute`
- `read_file`

## Examples

### Execute A Workspace Script

```text
execute(command="python3 /workspaces/{agent_id}/code/calculate.py")
```
