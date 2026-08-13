from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from knowcoder_workspace_builder.utils.agent_utils.message_validator.delegation import (
    _bind_task_payload,
)


@dataclass
class _Request:
    tool_call: dict[str, Any]

    def override(self, *, tool_call: dict[str, Any]) -> "_Request":
        return _Request(tool_call=tool_call)


def test_delegated_specialist_receives_stage_input_without_coordinator_instruction() -> None:
    request = _Request(
        tool_call={
            "name": "task",
            "args": {"subagent_type": "problem_clarifier", "description": "placeholder"},
        }
    )
    payload = {
        "stage": "problem",
        "input": {"question": "research question"},
        "coordination": {"instruction": "Call task once."},
    }

    bound = _bind_task_payload(request, payload)
    description = json.loads(bound.tool_call["args"]["description"])

    assert description == {
        "stage": "problem",
        "input": {"question": "research question"},
    }
