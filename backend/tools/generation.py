"""Maintenance-plan generation tools for the master agent."""

from __future__ import annotations

from typing import Any, Callable

from agentscope.tool import ToolResponse

from services.plan_generation import (
    build_generation_orchestration_context,
    generate_docx_from_state,
)
from services.requirements import (
    build_missing_question,
    default_form_state,
    find_missing_fields,
)

from .responses import json_tool_response


def build_prepare_plan_context_tool(
    session: dict[str, Any],
    runtime: Any,
) -> Callable[[], Any]:
    async def prepare_plan_context() -> ToolResponse:
        """Prepare Skill registration context and RAG evidence for generation."""
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        if missing:
            return json_tool_response(
                {
                    "status": "need_more",
                    "missing_fields": missing,
                    "question": build_missing_question(missing),
                    "collected": state,
                }
            )
        orchestration = await build_generation_orchestration_context(state)
        session["orchestration"] = orchestration
        return json_tool_response(
            {
                "status": "ready",
                "skill_selection_mode": orchestration.get("skill_selection_mode"),
                "registered_skills_count": len(runtime.get_skill_registry().skills),
                "rag_enabled": orchestration["rag_enabled"],
                "rag_chunks_count": orchestration["rag_chunks_count"],
                "rag_status": orchestration.get("rag_status"),
                "subject_anchor": orchestration.get("subject_anchor"),
                "rag_chunk_previews": orchestration.get("rag_chunk_previews", []),
            }
        )

    return prepare_plan_context


def build_generate_maintenance_plan_tool(
    session: dict[str, Any],
    runtime: Any,
) -> Callable[[str], Any]:
    async def generate_maintenance_plan(edit_instruction: str = "") -> ToolResponse:
        """Generate a maintenance-plan DOCX from the current collected state.

        Args:
            edit_instruction: Optional user instruction when revising an
                existing plan. Leave empty for new generation.
        """
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        if missing:
            return json_tool_response(
                {
                    "status": "need_more",
                    "missing_fields": missing,
                    "question": build_missing_question(missing),
                    "collected": state,
                }
            )
        orchestration = session.get("orchestration")
        if not orchestration:
            orchestration = await build_generation_orchestration_context(state)
            session["orchestration"] = orchestration
        file_id, _path, filename = await generate_docx_from_state(
            state,
            orchestration=orchestration,
            edit_instruction=edit_instruction,
        )
        generated = {
            "file_id": file_id,
            "filename": filename,
            "download_url": f"/api/download/{file_id}",
        }
        session["generated"] = generated
        return json_tool_response(
            {
                "status": "generated",
                **generated,
                "collected": state,
                "evidence": {
                    "skill_selection_mode": orchestration.get("skill_selection_mode"),
                    "registered_skills_count": len(runtime.get_skill_registry().skills),
                    "rag_enabled": orchestration["rag_enabled"],
                    "rag_chunks_count": orchestration["rag_chunks_count"],
                    "rag_status": orchestration.get("rag_status"),
                    "subject_anchor": orchestration.get("subject_anchor"),
                    "rag_chunk_previews": orchestration.get("rag_chunk_previews", []),
                },
            }
        )

    return generate_maintenance_plan
