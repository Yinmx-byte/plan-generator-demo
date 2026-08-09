"""Master ReActAgent for planner-first conversation and task routing."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.tool import Toolkit, ToolResponse

from agents.plan_agent import format_agent_trace
from services.requirements import default_form_state
from tools.master_toolkit import build_master_toolkit


MASTER_SYSTEM_PROMPT = (
    "你是国网云平台检修自主规划 Master Agent。"
    "业务流程、场景判断和工具调用顺序只以已注册的 AgentScope Skills 为准，"
    "其中 cloud-maintenance-master-workflow 是主控工作流的唯一规则来源。"
    "根据 Skill 目录摘要判断并读取所需 SKILL.md，再自主选择已注册工具完成任务。"
    "只能通过已注册工具执行受控动作，不得执行真实生产变更。"
    "不得暴露内部推理、工作流分析或隐藏上下文；用户明确询问系统架构时，可以解释公开的技术实现。"
    "最终回答必须且只能写在 <user_answer> 与 </user_answer> 标签之间，"
    "标签内直接给出回答或工具产出的用户结果，不得添加内部执行规则说明。"
)

USER_ANSWER_RE = re.compile(
    r"<user_answer>\s*(.*?)(?:\s*</user_answer>|\Z)",
    re.DOTALL,
)

SIMPLE_GREETINGS = {
    "你好",
    "您好",
    "hello",
    "hi",
    "hey",
    "在吗",
}
SIMPLE_TEST_MESSAGES = {"test", "测试"}


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
        max_iters=int(os.getenv("MASTER_AGENT_MAX_ITERS", "12")),
    )
    agent.set_console_output_enabled(False)
    return agent


async def run_master_agent_turn(
    message: str,
    session: dict[str, Any],
    runtime: MasterAgentRuntime,
    trace_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Any:
    """Run one autonomous master-agent turn, optionally streaming traces."""
    session.setdefault("state", default_form_state())
    session.setdefault("history", [])
    session["history"].append({"role": "user", "content": message})
    simple_reply = get_simple_direct_reply(message)
    if simple_reply:
        response = Msg("assistant", simple_reply, "assistant")
        session["history"].append({"role": "assistant", "content": simple_reply})
        return response
    state_preview = {
        key: value
        for key, value in session.get("state", {}).items()
        if isinstance(value, str) and value.strip()
    }
    generated = session.get("generated") or {}
    prompt = f"""请读取并遵循已注册的 cloud-maintenance-master-workflow Skill，处理本轮用户消息。

<conversation_context>
已确认的检修需求字段：
{json.dumps(state_preview, ensure_ascii=False, indent=2)}

是否已有生成文档：{bool(generated)}
上一版生成文档元数据：{json.dumps(generated, ensure_ascii=False)}
</conversation_context>

<user_message>
{message}
</user_message>

会话上下文只用于辅助判断，不得把未确认字段当成用户事实。最终只返回面向用户的回答。
"""
    agent = await create_master_agent(session, runtime)
    if not trace_callback:
        response = await agent(Msg("user", prompt, "user"))
        text = normalize_user_answer(response, runtime.get_response_text)
        session["history"].append({"role": "assistant", "content": text})
        return response

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    agent.set_msg_queue_enabled(True, queue)
    task = asyncio.create_task(agent(Msg("user", prompt, "user")))

    try:
        while True:
            if task.done() and queue.empty():
                break
            try:
                msg, last, _speech = await asyncio.wait_for(queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            for trace in format_agent_trace(msg, runtime.get_response_text, last):
                await trace_callback(trace)

        response = await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    text = normalize_user_answer(response, runtime.get_response_text)
    session["history"].append({"role": "assistant", "content": text})
    return response


def normalize_user_answer(
    response: Any,
    get_response_text: Callable[[Any], str],
) -> str:
    """Keep the explicit user-facing answer while preserving traces elsewhere."""
    text = get_response_text(response).strip()
    match = USER_ANSWER_RE.search(text)
    if match:
        text = match.group(1).strip()
        response.content = text
    return text


def get_simple_direct_reply(message: str) -> str:
    """Return a direct reply for standalone greetings/tests."""
    normalized = message.strip().lower().strip("。！？!?,，~～ ")
    if normalized in SIMPLE_GREETINGS:
        return "你好！"
    if normalized in SIMPLE_TEST_MESSAGES:
        return "收到。"
    return ""
