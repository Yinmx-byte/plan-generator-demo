"""Shared ToolResponse helpers."""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse


def json_tool_response(payload: dict[str, Any]) -> ToolResponse:
    """Return a JSON payload in AgentScope ToolResponse format."""
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        ]
    )

