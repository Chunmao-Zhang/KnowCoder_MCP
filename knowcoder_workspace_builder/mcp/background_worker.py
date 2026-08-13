"""One-shot entrypoint for a detached MCP Builder phase."""

from __future__ import annotations

import sys

from .background import run_background_worker


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: background_worker <workspace_id> <job_id>")
    raise SystemExit(run_background_worker(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()
