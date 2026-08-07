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
        sys_prompt=(
            "你是国网云平台检修自主规划 Master Agent。"
            "遵循已注册的 AgentScope Skills，尤其是 cloud-maintenance-master-workflow。"
            "你需要根据用户目标自主决定聊天、追问、抽取需求、准备依据或生成文档。"
            "你还可以盘点历史归档或查询相似检修记录，为用户提供准确统计和历史参考。"
            "你还可以分析检修操作对同 VPC 内关联资源的潜在影响，输出影响分析报告。"
            "只允许通过已注册工具执行受控动作。"
            "普通聊天只输出自然回复，不要输出意图判断、协议名称、会话字段或工具调用分析。"
            "云资源问数也只输出查询结论、关键数据和必要口径说明，不要输出读取 Skill、判断意图、工作流分析或内部推理。"
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
    prompt = f"""请执行 cloud-maintenance-master-workflow Skill 的自主规划协议。
当前会话已收集字段：
{json.dumps(state_preview, ensure_ascii=False, indent=2)}

当前是否已有生成文档：{bool(generated)}
上一版生成文档：{json.dumps(generated, ensure_ascii=False)}

用户最新消息：
{message}

要求：
- 普通聊天直接回答。
- 普通聊天不要输出“根据工作流协议/当前会话状态/结论/无需调用工具”等判断过程。
- 普通聊天不要主动列出已收集字段，也不要引导用户生成检修方案，除非用户主动询问。
- 云资源问数不要输出“我读取了 workflow/根据协议/我先判断/这属于某场景”等内部过程；只回答查询结果。
- 如果用户提供 ECS 实例 ID、VPC ID、资源组 ID，且询问“有没有、在哪、状态、资源占用、使用率、归属、详情”等，必须按云资源问数处理并调用 cloud_query 工具，不要当普通聊天。
- 当云监控指标返回 status=no_data 时，不要断言实例未安装插件或指标不可用的唯一原因；按工具 diagnosis 说明“当前无采样点、无法得出该指标使用率”，并列出可能原因和建议核查项。
- 当用户询问归档文档“有多少、有哪些、是否只有这些、清单是否完整”等盘点问题时，必须调用 list_maintenance_archives；该工具会同时返回全部版本记录数和按方案系列去重后的当前方案数。不得根据上一轮列出的部分记录自行推断总数。
- 当用户要搜索同类/相似检修记录、历史做法或历史规律时，调用 lookup_maintenance_history。归档盘点和相似记录分析都不是生成新方案，不要走需求提取流程。
- 检修方案相关任务（用户明确要求"生成/创建/写/做/帮我弄"新方案）必须先用工具更新、检查需求字段。
- 缺字段时直接向用户追问，不要生成文档。
- 字段完整后自主准备 Skill/RAG 依据并生成 DOCX。
- 如果已收集到产品类型、动作、系统名称中至少两项，可在生成方案前调用 lookup_maintenance_history 提供历史参考。历史查询结果中的 report 字段是结构化分析，自然融入回复即可。历史查询是可选辅助功能，归档库为空不影响正常生成。
- 当用户明确要求"分析影响/影响分析/有什么影响/会影响哪些/影响范围"时，调用 analyze_maintenance_impact 工具，在对话流中输出影响分析报告。工具会自动从会话状态提取实例 ID，无需重复询问。
- 当用户要求生成检修方案（未提及影响分析），方案生成完毕后，可以追问："需要我对这次检修做影响分析吗？"
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
    text = runtime.get_response_text(response)
    session["history"].append({"role": "assistant", "content": text})
    return response


def get_simple_direct_reply(message: str) -> str:
    """Return a direct reply for standalone greetings/tests."""
    normalized = message.strip().lower().strip("。！？!?,，~～ ")
    if normalized in SIMPLE_GREETINGS:
        return "你好！"
    if normalized in SIMPLE_TEST_MESSAGES:
        return "收到。"
    return ""
