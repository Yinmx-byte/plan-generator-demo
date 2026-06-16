"""Context and knowledge tools for the master agent."""

from __future__ import annotations

from typing import Any, Callable

from agentscope.tool import ToolResponse

from rag import get_knowledge_base
from runtime import SKILLS_ROOT
from services.requirements import default_form_state

from .responses import json_tool_response


def build_get_session_snapshot_tool(session: dict[str, Any]) -> Callable[[], ToolResponse]:
    def get_session_snapshot() -> ToolResponse:
        """Return current session state, generated document info and recent history."""
        state = session.setdefault("state", default_form_state())
        return json_tool_response(
            {
                "status": "ok",
                "collected": state,
                "generated": session.get("generated"),
                "orchestration_ready": bool(session.get("orchestration")),
                "recent_history": session.get("history", [])[-6:],
            }
        )

    return get_session_snapshot


def build_list_registered_skills_tool(runtime: Any) -> Callable[[str], ToolResponse]:
    def list_registered_skills(filter_text: str = "") -> ToolResponse:
        """List registered AgentScope Skills.

        Args:
            filter_text: Optional keyword used to filter skill name or description.
        """
        keyword = filter_text.strip().lower()
        skills = []
        for skill in runtime.get_skill_registry().skills:
            haystack = f"{skill.name}\n{skill.description}".lower()
            if keyword and keyword not in haystack:
                continue
            skills.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "skill_dir": str(skill.path),
                    "skill_file": str(skill.path / "SKILL.md"),
                }
            )
        return json_tool_response({"status": "ok", "skills": skills, "count": len(skills)})

    return list_registered_skills


def build_retrieve_knowledge_tool() -> Callable[[str, int], Any]:
    async def retrieve_knowledge(query: str, top_k: int = 5) -> ToolResponse:
        """Retrieve remote RAG knowledge chunks from the configured knowledge base.

        Args:
            query: Retrieval query.
            top_k: Maximum number of chunks to return.
        """
        knowledge_base = get_knowledge_base(SKILLS_ROOT)
        if knowledge_base is None:
            return json_tool_response(
                {
                    "status": "disabled",
                    "chunks": [],
                    "message": "RAG 未启用或百炼知识库配置不完整。",
                }
            )
        chunks = await knowledge_base.retrieve(query, top_k=top_k)
        return json_tool_response(
            {
                "status": "ok",
                "query": query,
                "chunks": chunks,
                "count": len(chunks),
            }
        )

    return retrieve_knowledge

