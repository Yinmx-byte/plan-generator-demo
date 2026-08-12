"""In-process chat sessions and session-scoped Master Agent lifecycle."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from agentscope.message import Msg

from services.requirements import default_form_state


MasterAgentFactory = Callable[[dict[str, Any], Any], Awaitable[Any]]


class ChatSessionStore:
    """Keep authoritative state and reusable Agent instances per session."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self._agent_revision = 0

    def get_or_create(self, session_id: str) -> dict[str, Any]:
        return self.sessions.setdefault(
            session_id,
            {
                "state": default_form_state(),
                "history": [],
                "generated": None,
                "_master_agent": None,
                "_master_agent_revision": -1,
                "_master_agent_lock": asyncio.Lock(),
            },
        )

    def invalidate_master_agents(self) -> None:
        """Make all cached agents rebuild before their next turn."""
        self._agent_revision += 1

    async def get_master_agent(
        self,
        session: dict[str, Any],
        runtime: Any,
        factory: MasterAgentFactory,
    ) -> Any:
        agent = session.get("_master_agent")
        if (
            agent is not None
            and session.get("_master_agent_revision") == self._agent_revision
        ):
            return agent

        previous_agent = agent
        agent = await factory(session, runtime)
        await self._restore_recent_history(agent, session.get("history", []))
        session["_master_agent"] = agent
        session["_master_agent_revision"] = self._agent_revision
        if previous_agent is not None:
            await previous_agent.interrupt()
            await previous_agent.memory.clear()
        return agent

    @staticmethod
    def append_history(
        session: dict[str, Any],
        role: str,
        content: str,
    ) -> None:
        """Append bounded external history used to rebuild an invalidated Agent."""
        history = session.setdefault("history", [])
        history.append({"role": role, "content": content})
        limit = max(2, int(os.getenv("CHAT_SESSION_HISTORY_LIMIT", "100")))
        if len(history) > limit:
            del history[:-limit]

    async def reset(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if not session:
            return
        agent = session.get("_master_agent")
        if agent is not None:
            await agent.interrupt()
            await agent.memory.clear()

    async def clear(self) -> None:
        session_ids = list(self.sessions)
        for session_id in session_ids:
            await self.reset(session_id)

    @staticmethod
    async def _restore_recent_history(agent: Any, history: list[dict[str, Any]]) -> None:
        limit = max(0, int(os.getenv("MASTER_AGENT_HISTORY_RESTORE_LIMIT", "30")))
        entries = history[-limit:] if limit else []
        messages = []
        for item in entries:
            role = item.get("role")
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            messages.append(Msg(role, content, role))
        if messages:
            await agent.memory.add(messages)


_chat_session_store = ChatSessionStore()


def get_chat_session_store() -> ChatSessionStore:
    return _chat_session_store
