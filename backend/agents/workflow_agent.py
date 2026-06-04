"""Workflow controller agent helpers.

The workflow agent is the single conversation entry point. It returns one
structured decision per user turn: either a direct chat answer, or a dispatch
decision for the plan-generation pipeline.
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg


@dataclass(frozen=True)
class WorkflowAgentRuntime:
    get_model: Callable[[], Any]
    get_formatter: Callable[[], Any]
    get_toolkit: Callable[[], Awaitable[Any]]
    extract_json: Callable[[str], dict[str, Any]]
    get_response_text: Callable[[Any], str]


async def create_workflow_agent(runtime: WorkflowAgentRuntime) -> ReActAgent:
    """Create the native AgentScope workflow/controller ReActAgent."""
    agent = ReActAgent(
        name="MaintenancePlanWorkflowController",
        sys_prompt="你是检修方案生成系统的总控 Agent。遵循已注册的 AgentScope Skills。",
        model=runtime.get_model(),
        formatter=runtime.get_formatter(),
        toolkit=await runtime.get_toolkit(),
        memory=InMemoryMemory(),
        max_iters=int(os.getenv("WORKFLOW_AGENT_MAX_ITERS", "4")),
    )
    agent.set_console_output_enabled(False)
    return agent


def normalize_workflow_result(data: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    """Normalize the workflow agent JSON into a safe dispatch object."""
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in {"chat", "generate", "regenerate", "edit"}:
        intent = "chat"

    if not session.get("generated") and intent in {"edit", "regenerate"}:
        intent = "generate" if intent == "regenerate" else "chat"

    should_extract = data.get("should_extract")
    if not isinstance(should_extract, bool):
        should_extract = intent in {"generate", "edit"}
    if intent == "chat":
        should_extract = False

    assistant_message = str(data.get("assistant_message", "")).strip()
    if intent == "chat" and not assistant_message:
        assistant_message = "我在。你可以直接描述检修需求，也可以问我这个系统怎么工作。"

    return {
        "intent": intent,
        "should_extract": should_extract,
        "assistant_message": assistant_message,
        "reason": str(data.get("reason", ""))[:200],
    }


async def run_workflow_turn(
    message: str,
    session: dict[str, Any],
    runtime: WorkflowAgentRuntime,
) -> dict[str, Any]:
    """Run the workflow agent once for intent routing or direct chat answer."""
    fallback = {
        "intent": "chat",
        "should_extract": False,
        "assistant_message": "我刚才没能完成意图识别，请再发一次需求或问题。",
        "reason": "workflow_agent_failed",
    }
    state_preview = {
        key: value
        for key, value in session.get("state", {}).items()
        if isinstance(value, str) and value.strip()
    }
    history = session.get("history", [])[-8:]
    history_text = "\n".join(
        f"{item.get('role', 'user')}: {item.get('content', '')}" for item in history
    )
    prompt = f"""请执行 maintenance-plan-workflow Skill 的入口协议。
只返回一个 JSON 对象，不要输出 markdown，不要解释。
会话是否已有生成文档：{bool(session.get("generated"))}
当前已收集字段：{json.dumps(state_preview, ensure_ascii=False)}
最近对话：
{history_text}
用户最新消息：{message}
"""
    try:
        agent = await create_workflow_agent(runtime)
        response = await agent(Msg("user", prompt, "user"))
        data = runtime.extract_json(runtime.get_response_text(response))
    except Exception:
        return fallback

    return normalize_workflow_result(data, session)
