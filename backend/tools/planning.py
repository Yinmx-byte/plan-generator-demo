"""Requirement planning tools for the master agent."""

from __future__ import annotations

from typing import Any, Callable

from agentscope.tool import ToolResponse

from services.requirements import (
    build_missing_question,
    default_form_state,
    extract_chat_updates,
    find_missing_fields,
    merge_updates,
)

from .responses import json_tool_response


def build_update_requirements_tool(session: dict[str, Any]) -> Callable[[str], Any]:
    async def update_requirements(message: str) -> ToolResponse:
        """Extract maintenance-plan requirement fields from a user message.

        Args:
            message: Latest user message that may contain maintenance-plan
                requirements or corrections.
        """
        state = session.setdefault("state", default_form_state())
        extracted = await extract_chat_updates(state, message)
        merge_updates(state, extracted.get("updates", {}))
        return json_tool_response(
            {
                "status": "updated",
                "updates": extracted.get("updates", {}),
                "assistant_note": extracted.get("assistant_note", ""),
                "collected": state,
            }
        )

    return update_requirements


def build_check_missing_requirements_tool(session: dict[str, Any]) -> Callable[[], ToolResponse]:
    def check_missing_requirements() -> ToolResponse:
        """Check whether the current session has all required plan fields."""
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        return json_tool_response(
            {
                "status": "need_more" if missing else "complete",
                "missing_fields": missing,
                "question": build_missing_question(missing),
                "collected": state,
            }
        )

    return check_missing_requirements

