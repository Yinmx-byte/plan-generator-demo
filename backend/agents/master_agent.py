"""Master ReActAgent for planner-first conversation and task routing."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.tool import Toolkit, ToolResponse

from agents.plan_agent import format_agent_trace
from services.requirements import default_form_state
from tools.master_toolkit import build_master_toolkit


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
        sys_prompt=(
            "你是国网云平台检修自主规划 Master Agent。"
            "遵循已注册的 AgentScope Skills，尤其是 cloud-maintenance-master-workflow。"
            "你需要根据用户目标自主决定聊天、追问、抽取需求、准备依据或生成文档。"
            "只允许通过已注册工具执行受控动作。"
        ),
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
    state_preview = {
        key: value
        for key, value in session.get("state", {}).items()
        if isinstance(value, str) and value.strip()
    }
    generated = session.get("generated") or {}
    prompt = f"""请执行 cloud-maintenance-master-workflow Skill 的自主规划协议。
当前会话已收集字段：
{json.dumps(state_preview, ensure_ascii=False, indent=2)}

当前是否已有生成文档：{bool(generated)}
上一版生成文档：{json.dumps(generated, ensure_ascii=False)}

用户最新消息：
{message}

要求：
- 普通聊天直接回答。
- 检修方案相关任务必须先用工具更新、检查需求字段。
- 缺字段时直接向用户追问，不要生成文档。
- 字段完整后自主准备 Skill/RAG 依据并生成 DOCX。
- 最终回复要说明执行了哪些关键步骤；如果生成了 DOCX，必须给出 download_url。
"""
    agent = await create_master_agent(session, runtime)
    if not trace_callback:
        response = await agent(Msg("user", prompt, "user"))
        text = runtime.get_response_text(response)
        session["history"].append({"role": "assistant", "content": text})
        return response

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    agent.set_msg_queue_enabled(True, queue)
    task = asyncio.create_task(agent(Msg("user", prompt, "user")))

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
    text = runtime.get_response_text(response)
    session["history"].append({"role": "assistant", "content": text})
    return response
