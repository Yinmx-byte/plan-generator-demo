"""Page Agent MCP integration helpers."""

import inspect
import os
import uuid
from typing import Any

from fastapi import HTTPException
from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.tool import ToolResponse

from runtime import get_formatter, get_model, get_toolkit
from services.json_utils import get_response_text

async def run_plan_validation_agent(
    state: dict[str, str],
    filename: str,
    download_url: str,
) -> str:
    """Use Page Agent MCP to validate the generated plan from a browser."""
    agent = ReActAgent(
        name="MaintenancePlanValidator",
        sys_prompt=(
            "你是检修方案浏览器验证助手。你只能做只读验证和页面操作验证，"
            "不得执行任何真实生产变更、删除、重启、扩缩容、创建资源等不可逆操作。"
            "如果 Page Agent Hub 未连接，直接说明需要在 Chrome 中允许 Page Agent 扩展连接。"
        ),
        model=get_model(),
        formatter=get_formatter(),
        toolkit=await get_toolkit(),
        memory=InMemoryMemory(),
        max_iters=int(os.getenv("VALIDATION_AGENT_MAX_ITERS", "6")),
    )
    agent.set_console_output_enabled(False)
    prompt = f"""请通过 Page Agent MCP 对刚生成的检修方案做浏览器侧验证。

验证范围：
1. 先调用 Page Agent 状态工具确认浏览器 Hub 是否连接。
2. 如果已连接，打开 http://127.0.0.1:8000{download_url}，确认检修方案文档可访问或可下载。
3. 不要执行文档中的真实检修命令，不要登录生产系统，不要进行任何实际资源变更。
4. 输出验证结论、发现的问题和下一步人工检查建议。

生成文件：{filename}
检修类型：{state.get("maintenance_type", "")}
检修背景：{state.get("background", "")}
涉及实例：{state.get("instances", "")}
"""
    response = await agent(Msg("user", prompt, "user"))
    return get_response_text(response)


def tool_response_text(response: ToolResponse) -> str:
    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(str(text))
    return "\n".join(parts).strip()


async def run_page_agent_task(task: str) -> str:
    task = task.strip()
    if not task:
        raise HTTPException(status_code=400, detail="请输入 Page Agent 测试指令")
    toolkit = await get_toolkit()
    tool_name = "execute_task"
    if tool_name not in toolkit.tools:
        raise HTTPException(
            status_code=503,
            detail="Page Agent MCP 工具未注册，请检查 backend/mcp_servers.json。",
        )
    tool_call = {
        "type": "tool_use",
        "id": uuid.uuid4().hex,
        "name": tool_name,
        "input": {"task": task},
    }
    result = ""
    tool_result = toolkit.call_tool_function(tool_call)
    if inspect.isawaitable(tool_result):
        tool_result = await tool_result
    async for chunk in tool_result:
        result = tool_response_text(chunk) or result
    return result or "Page Agent 执行完成，但未返回文本结果。"


# ── API 路由 ────────────────────────────────────────────────────


