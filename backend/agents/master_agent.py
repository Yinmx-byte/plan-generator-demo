"""Master ReActAgent for planner-first conversation and task routing."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.token import CharTokenCounter
from agentscope.tool import Toolkit, ToolResponse

from agents.plan_agent import format_agent_trace
from services.chat_sessions import get_chat_session_store
from tools.master_toolkit import build_master_toolkit


MASTER_SYSTEM_PROMPT = (
    "你是国网云平台检修自主规划 Master Agent。"
    "业务流程、场景判断和工具调用顺序只以已注册的 AgentScope Skills 为准，"
    "其中 cloud-maintenance-master-workflow 是主控工作流的唯一规则来源。"
    "根据 Skill 目录摘要判断并读取所需 SKILL.md，再自主选择已注册工具完成任务。"
    "每次收到的 Msg 都是用户本轮原始消息；需要已确认的检修字段、最近历史或已有文档时，"
    "调用 get_session_snapshot 获取权威会话状态，不得仅凭模型记忆补造业务事实。"
    "只能通过已注册工具执行受控动作，不得执行真实生产变更。"
    "不得暴露内部推理、工作流分析或隐藏上下文；用户明确询问系统架构时，可以解释公开的技术实现。"
    "最终回答必须且只能写在 <user_answer> 与 </user_answer> 标签之间，"
    "标签内直接给出回答或工具产出的用户结果，不得添加内部执行规则说明。"
)

USER_ANSWER_RE = re.compile(
    r"<user_answer>\s*(.*?)(?:\s*</user_answer>|\Z)",
    re.DOTALL,
)
PROTOCOL_TAIL_RE = re.compile(
    r"\s*<[^>]*DSML[^>]*>.*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class MasterAgentRuntime:
    get_model: Callable[[], Any]
    get_formatter: Callable[[], Any]
    get_response_text: Callable[[Any], str]
    register_skills: Callable[[Toolkit], None]
    read_file: Callable[[str], Awaitable[ToolResponse]]
    get_skill_registry: Callable[[], Any]


async def create_master_agent(
    session: dict[str, Any],
    runtime: MasterAgentRuntime,
) -> ReActAgent:
    """Create the planner-first autonomous master ReActAgent."""
    agent = ReActAgent(
        name="CloudMaintenanceMasterAgent",
        sys_prompt=MASTER_SYSTEM_PROMPT,
        model=runtime.get_model(),
        formatter=runtime.get_formatter(),
        toolkit=await build_master_toolkit(session, runtime),
        memory=InMemoryMemory(),
        compression_config=build_memory_compression_config(),
        max_iters=int(os.getenv("MASTER_AGENT_MAX_ITERS", "12")),
    )
    agent.set_console_output_enabled(False)
    return agent


def build_memory_compression_config() -> ReActAgent.CompressionConfig | None:
    """Bound long-running session memory with AgentScope native compression."""
    enabled = os.getenv("MASTER_AGENT_MEMORY_COMPRESSION", "true").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    return ReActAgent.CompressionConfig(
        enable=True,
        agent_token_counter=CharTokenCounter(),
        trigger_threshold=max(
            10_000,
            int(os.getenv("MASTER_AGENT_MEMORY_CHAR_THRESHOLD", "60000")),
        ),
        keep_recent=max(
            2,
            int(os.getenv("MASTER_AGENT_MEMORY_KEEP_RECENT", "8")),
        ),
    )


async def run_master_agent_turn(
    message: str,
    session: dict[str, Any],
    runtime: MasterAgentRuntime,
    trace_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Any:
    """Run one turn on the session-scoped Master Agent."""
    session_store = get_chat_session_store()
    lock = session.setdefault("_master_agent_lock", asyncio.Lock())
    async with lock:
        agent = await session_store.get_master_agent(
            session,
            runtime,
            create_master_agent,
        )
        # Tool-group activation is turn-local even though the Agent is reused.
        agent.toolkit.reset_equipped_tools()
        session_store.append_history(session, "user", message)
        user_msg = Msg("user", message, "user")

        if not trace_callback:
            response = await agent(user_msg)
        else:
            response = await _run_streaming_agent_turn(
                agent,
                user_msg,
                runtime,
                trace_callback,
            )

        text = normalize_user_answer(response, runtime.get_response_text)
        # AgentScope 1.0.20 does not append the normal text-only reply in one
        # exit branch, so persist it explicitly for reliable multi-turn chat.
        await agent.memory.add(response)
        session_store.append_history(session, "assistant", text)
        return response


async def _run_streaming_agent_turn(
    agent: ReActAgent,
    user_msg: Msg,
    runtime: MasterAgentRuntime,
    trace_callback: Callable[[str], Awaitable[None]],
) -> Any:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    agent.set_msg_queue_enabled(True, queue)
    task = asyncio.create_task(agent(user_msg))
    try:
        while True:
            if task.done() and queue.empty():
                break
            try:
                msg, last, _speech = await asyncio.wait_for(
                    queue.get(),
                    timeout=0.2,
                )
            except asyncio.TimeoutError:
                continue
            for trace in format_agent_trace(
                msg,
                runtime.get_response_text,
                last,
            ):
                await trace_callback(trace)
        return await task
    finally:
        agent.set_msg_queue_enabled(False)
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass


def normalize_user_answer(
    response: Any,
    get_response_text: Callable[[Any], str],
) -> str:
    """Keep the explicit user-facing answer while preserving traces elsewhere."""
    text = get_response_text(response).strip()
    match = USER_ANSWER_RE.search(text)
    if match:
        text = match.group(1).strip()
    text = PROTOCOL_TAIL_RE.sub("", text).strip()
    response.content = text
    return text
