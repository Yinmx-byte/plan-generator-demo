"""Plan-writing AgentScope runtime used behind the plan generation service."""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg


@dataclass(frozen=True)
class PlanAgentRuntime:
    build_system_prompt: Callable[[], str]
    get_model: Callable[[], Any]
    get_formatter: Callable[[], Any]
    get_toolkit: Callable[[], Awaitable[Any]]
    get_agent_knowledge: Callable[[], Awaitable[Optional[Any]]]
    get_response_text: Callable[[Any], str]


async def create_plan_agent(runtime: PlanAgentRuntime) -> ReActAgent:
    """Create the native AgentScope ReActAgent for plan generation."""
    agent = ReActAgent(
        name="MaintenancePlanGenerator",
        sys_prompt=runtime.build_system_prompt(),
        model=runtime.get_model(),
        formatter=runtime.get_formatter(),
        toolkit=await runtime.get_toolkit(),
        memory=InMemoryMemory(),
        knowledge=await runtime.get_agent_knowledge(),
        enable_rewrite_query=True,
        max_iters=int(os.getenv("AGENT_MAX_ITERS", "8")),
    )
    agent.set_console_output_enabled(False)
    return agent


def format_agent_trace(
    msg: Msg,
    get_response_text: Callable[[Any], str],
    last: bool = True,
) -> list[str]:
    """Convert AgentScope message blocks into frontend-readable trace text."""
    traces: list[str] = []
    try:
        blocks = msg.get_content_blocks()
    except Exception:
        return traces
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_use":
            if block.get("name") == "read_file":
                continue
            tool_input = json.dumps(block.get("input", {}), ensure_ascii=False)
            traces.append(f"调用工具：{block.get('name', 'unknown')}\n参数：{tool_input[:600]}")
        elif block_type == "tool_result":
            if block.get("name") == "read_file":
                continue
            output = get_response_text(block.get("output", ""))
            traces.append(f"工具返回：{block.get('name', 'unknown')}\n{output[:800]}")
        elif block_type == "thinking":
            if os.getenv("SHOW_AGENT_THINKING_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}:
                traces.append(f"模型思考：{str(block.get('thinking', ''))[:800]}")
        elif block_type == "text":
            if os.getenv("SHOW_AGENT_TEXT_TRACE", "").strip().lower() not in {"1", "true", "yes", "on"}:
                continue
            text = str(block.get("text", "")).strip()
            if text:
                label = "模型输出完成" if last else "模型输出中"
                traces.append(f"{label}：{text[:800]}")
    return traces


async def run_plan_agent(
    user_prompt: str,
    runtime: PlanAgentRuntime,
    trace_callback: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Any:
    """Run the native AgentScope ReActAgent for plan generation."""
    agent = await create_plan_agent(runtime)
    if not trace_callback:
        return await agent(Msg("user", user_prompt, "user"))

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    agent.set_msg_queue_enabled(True, queue)
    task = asyncio.create_task(agent(Msg("user", user_prompt, "user")))

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
        return await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
