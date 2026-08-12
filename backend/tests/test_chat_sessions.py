"""Session-scoped Master Agent lifecycle tests."""

from __future__ import annotations

import unittest
import sys
import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agentscope.message import Msg

from agents.master_agent import MasterAgentRuntime, run_master_agent_turn
from services import chat_sessions
from services.chat_sessions import ChatSessionStore


class FakeMemory:
    def __init__(self) -> None:
        self.messages: list[Msg] = []
        self.cleared = False

    async def add(self, messages) -> None:
        if isinstance(messages, list):
            self.messages.extend(messages)
        else:
            self.messages.append(messages)

    async def clear(self) -> None:
        self.cleared = True
        self.messages.clear()


class FakeToolkit:
    def __init__(self) -> None:
        self.reset_count = 0

    def reset_equipped_tools(self) -> None:
        self.reset_count += 1


class FakeAgent:
    def __init__(self) -> None:
        self.memory = FakeMemory()
        self.toolkit = FakeToolkit()
        self.inputs: list[str] = []
        self.interrupted = False

    async def __call__(self, msg: Msg) -> Msg:
        self.inputs.append(msg.get_text_content())
        await self.memory.add(msg)
        response = Msg("assistant", f"<user_answer>回答：{msg.get_text_content()}</user_answer>", "assistant")
        await self.memory.add(response)
        return response

    async def interrupt(self) -> None:
        self.interrupted = True


class ChatSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_is_reused_and_rebuilt_after_revision_change(self) -> None:
        store = ChatSessionStore()
        session = store.get_or_create("same-session")
        created: list[FakeAgent] = []

        async def factory(_session, _runtime):
            agent = FakeAgent()
            created.append(agent)
            return agent

        first = await store.get_master_agent(session, None, factory)
        second = await store.get_master_agent(session, None, factory)
        self.assertIs(first, second)
        self.assertEqual(len(created), 1)

        session["history"] = [
            {"role": "user", "content": "历史问题"},
            {"role": "assistant", "content": "历史回答"},
        ]
        store.invalidate_master_agents()
        rebuilt = await store.get_master_agent(session, None, factory)
        self.assertIsNot(first, rebuilt)
        self.assertTrue(first.memory.cleared)
        self.assertEqual(
            [item.get_text_content() for item in rebuilt.memory.messages],
            ["历史问题", "历史回答"],
        )

    async def test_reset_interrupts_and_removes_session(self) -> None:
        store = ChatSessionStore()
        session = store.get_or_create("reset-session")
        agent = FakeAgent()
        session["_master_agent"] = agent
        await store.reset("reset-session")
        self.assertTrue(agent.interrupted)
        self.assertTrue(agent.memory.cleared)
        self.assertNotIn("reset-session", store.sessions)

    async def test_different_sessions_do_not_share_agents_or_history(self) -> None:
        store = ChatSessionStore()
        created: list[FakeAgent] = []

        async def factory(_session, _runtime):
            agent = FakeAgent()
            created.append(agent)
            return agent

        first_session = store.get_or_create("session-a")
        second_session = store.get_or_create("session-b")
        first_agent = await store.get_master_agent(first_session, None, factory)
        second_agent = await store.get_master_agent(second_session, None, factory)
        store.append_history(first_session, "user", "only in a")

        self.assertIsNot(first_agent, second_agent)
        self.assertEqual(second_session["history"], [])


class MasterAgentTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_turns_use_one_agent_and_pass_only_current_message(self) -> None:
        original_store = chat_sessions._chat_session_store
        store = ChatSessionStore()
        chat_sessions._chat_session_store = store
        agent = FakeAgent()
        created = 0

        async def factory(_session, _runtime):
            nonlocal created
            created += 1
            return agent

        runtime = MasterAgentRuntime(
            get_model=lambda: None,
            get_formatter=lambda: None,
            get_response_text=lambda response: response.get_text_content(),
            register_skills=lambda _toolkit: None,
            read_file=lambda _path: None,
            get_skill_registry=lambda: None,
        )
        session = store.get_or_create("turn-session")
        from agents import master_agent

        original_factory = master_agent.create_master_agent
        master_agent.create_master_agent = factory
        try:
            first = await run_master_agent_turn("第一轮", session, runtime)
            second = await run_master_agent_turn("第二轮", session, runtime)
        finally:
            master_agent.create_master_agent = original_factory
            chat_sessions._chat_session_store = original_store

        self.assertEqual(created, 1)
        self.assertEqual(agent.inputs, ["第一轮", "第二轮"])
        self.assertEqual(agent.toolkit.reset_count, 2)
        self.assertEqual(first.get_text_content(), "回答：第一轮")
        self.assertEqual(second.get_text_content(), "回答：第二轮")
        self.assertEqual(len(session["history"]), 4)

    async def test_external_history_is_bounded(self) -> None:
        store = ChatSessionStore()
        session = store.get_or_create("bounded-session")
        previous = os.environ.get("CHAT_SESSION_HISTORY_LIMIT")
        os.environ["CHAT_SESSION_HISTORY_LIMIT"] = "4"
        try:
            for index in range(6):
                store.append_history(session, "user", str(index))
        finally:
            if previous is None:
                os.environ.pop("CHAT_SESSION_HISTORY_LIMIT", None)
            else:
                os.environ["CHAT_SESSION_HISTORY_LIMIT"] = previous
        self.assertEqual(
            [entry["content"] for entry in session["history"]],
            ["2", "3", "4", "5"],
        )


if __name__ == "__main__":
    unittest.main()
