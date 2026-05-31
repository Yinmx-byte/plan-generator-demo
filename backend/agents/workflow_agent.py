"""Workflow controller agent helpers.

The workflow agent decides whether a user turn is ordinary chat, a new plan,
a regeneration, or an edit of the latest generated plan. It also handles the
ordinary-chat branch so the plan generator agent only writes plans.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class WorkflowAgentRuntime:
    get_model: Callable[[], Any]
    get_skill_registry: Callable[[], Any]
    extract_json: Callable[[str], dict[str, Any]]
    get_response_text: Callable[[Any], str]


def detect_chat_intent(message: str, session: dict[str, Any]) -> str:
    """Fallback heuristic used only when the workflow model cannot respond."""
    plan_keywords = (
        "检修方案",
        "生成方案",
        "生成一个",
        "出一份",
        "写一份",
        "创建ECS",
        "创建 ECS",
        "ecs",
        "ECS",
        "rds",
        "RDS",
        "redis",
        "Redis",
        "slb",
        "SLB",
        "oss",
        "OSS",
        "k8s",
        "K8S",
        "polardb",
        "PolarDB",
    )
    if not session.get("generated"):
        return "generate" if any(keyword in message for keyword in plan_keywords) else "chat"
    regenerate_keywords = (
        "重新生成",
        "重新出",
        "再生成",
        "重做",
        "重新来",
        "重新写",
        "生成一遍",
        "再来一版",
        "按原需求",
        "根据需求重新",
        "不是修订",
        "不是修改",
    )
    if any(keyword in message for keyword in regenerate_keywords):
        return "regenerate"
    edit_keywords = (
        "修改",
        "更改",
        "调整",
        "替换",
        "修订",
        "重新评估",
        "风险点",
        "人员名单",
        "检修人员",
        "执行人",
        "复核人",
        "安全责任人",
        "按照",
        "参考",
    )
    if any(keyword in message for keyword in edit_keywords):
        return "edit"
    return "generate" if any(keyword in message for keyword in plan_keywords) else "chat"


def get_workflow_skill_body(runtime: WorkflowAgentRuntime) -> str:
    skill = runtime.get_skill_registry().get("maintenance-plan-workflow")
    return skill.body if skill else ""


def should_extract_for_intent(message: str, intent: str) -> bool:
    """Regeneration-only commands should not overwrite collected requirements."""
    if intent != "regenerate":
        return True
    if "\n" in message:
        return True
    if any(mark in message for mark in ("：", ":", "；", ";")) and len(message) > 30:
        return True
    field_words = (
        "检修背景",
        "检修类型",
        "网络环境",
        "实施地点",
        "涉及实例",
        "检修窗口",
        "方案提供人",
        "检修执行人",
        "检修复核人",
        "安全责任人",
        "ASCM",
        "堡垒机",
        "技术参数",
    )
    return any(word in message for word in field_words)


async def classify_chat_intent(
    message: str,
    session: dict[str, Any],
    runtime: WorkflowAgentRuntime,
) -> dict[str, Any]:
    """Classify the latest user message before entering the plan workflow."""
    fallback_intent = detect_chat_intent(message, session)
    fallback = {
        "intent": fallback_intent,
        "should_extract": False
        if fallback_intent == "chat"
        else should_extract_for_intent(message, fallback_intent),
        "reason": "fallback",
    }
    state_preview = {
        key: value
        for key, value in session.get("state", {}).items()
        if isinstance(value, str) and value.strip()
    }
    workflow_skill = get_workflow_skill_body(runtime)
    prompt = f"""你是检修方案生成系统的总控工作流 Agent，请判断用户最新消息应该进入哪个流程。
请严格遵守下面的总控工作流 Skill：
{workflow_skill[:6000]}

只能返回 JSON，不要输出 markdown，不要解释。
可选 intent：
- chat：普通交流、询问系统能力、询问原理、打招呼、咨询配置/流程/如何使用；不生成或修改文档。
- generate：用户提供检修需求，或明确要求生成新的检修方案。
- regenerate：会话中已经有生成结果，用户要求“重新生成/再生成/重做/按原需求生成一遍”，且不是要求修改上一版内容。
- edit：会话中已经有生成结果，用户要求修改上一版文档，例如变更人员、替换内容、重新评估风险点、按某文档调整已有方案。
判断规则：
- 没有已生成文档时，不要返回 edit 或 regenerate。
- “重新生成一遍/根据需求重新生成”优先是 regenerate，不是 edit。
- “修改/调整/替换/重新评估风险点/变更检修人员名单/按照某文档修订”才是 edit。
- 普通聊天不要抽取需求字段。
- regenerate 如果只是短指令，不要抽取需求字段；如果用户同时贴了新的完整需求，可以抽取。
返回格式：
{{
  "intent": "chat|generate|regenerate|edit",
  "should_extract": true,
  "reason": "一句话说明"
}}

会话是否已有生成文档：{bool(session.get("generated"))}
当前已收集字段：{json.dumps(state_preview, ensure_ascii=False)}
用户最新消息：{message}
"""
    try:
        response = await runtime.get_model()(
            [
                {"role": "system", "content": "你只输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ],
        )
        data = runtime.extract_json(runtime.get_response_text(response))
    except Exception:
        return fallback

    intent = str(data.get("intent", "")).strip().lower()
    if intent not in {"chat", "generate", "regenerate", "edit"}:
        return fallback
    if not session.get("generated") and intent in {"edit", "regenerate"}:
        intent = "generate" if intent == "regenerate" else "chat"
    should_extract = data.get("should_extract")
    if not isinstance(should_extract, bool):
        should_extract = should_extract_for_intent(message, intent)
    if intent == "chat":
        should_extract = False
    return {
        "intent": intent,
        "should_extract": should_extract,
        "reason": str(data.get("reason", ""))[:200],
    }


async def run_normal_chat(
    session: dict[str, Any],
    message: str,
    runtime: WorkflowAgentRuntime,
) -> str:
    """Answer normal user messages without entering the plan generation chain."""
    state_preview = {
        key: value
        for key, value in session.get("state", {}).items()
        if isinstance(value, str) and value.strip()
    }
    history = session.get("history", [])[-8:]
    history_text = "\n".join(
        f"{item.get('role', 'user')}: {item.get('content', '')}" for item in history
    )
    prompt = f"""你是检修方案生成系统的总控工作流 Agent，可以正常交流，也可以说明系统如何生成、修订和验证检修方案。
当前已收集的方案字段（如有）：{json.dumps(state_preview, ensure_ascii=False)}
最近对话：
{history_text}

请直接回答用户最新问题。若用户是在询问如何生成方案，可以简要说明需要提供哪些信息；不要在普通聊天中生成 DOCX。
用户最新问题：{message}
"""
    response = await runtime.get_model()(
        [
            {
                "role": "system",
                "content": "你是检修方案生成系统的中文总控助手，回答要简洁准确。",
            },
            {"role": "user", "content": prompt},
        ],
    )
    return (
        runtime.get_response_text(response).strip()
        or "我在。你可以直接描述检修需求，也可以问我这个系统怎么工作。"
    )

