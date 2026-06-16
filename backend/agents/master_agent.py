"""Master ReActAgent for planner-first conversation and task routing."""

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agentscope.agent import ReActAgent
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg, TextBlock
from agentscope.tool import Toolkit, ToolResponse

from agents.plan_agent import format_agent_trace
from services.requirements import (
    build_missing_question,
    default_form_state,
    extract_chat_updates,
    find_missing_fields,
    merge_updates,
)
from services.plan_generation import (
    build_generation_orchestration_context,
    docx_to_markdown,
    generate_docx_from_state,
    get_generated_file,
)
from runtime import SKILLS_ROOT
from rag import get_knowledge_base


@dataclass(frozen=True)
class MasterAgentRuntime:
    get_model: Callable[[], Any]
    get_formatter: Callable[[], Any]
    get_response_text: Callable[[Any], str]
    register_skills: Callable[[Toolkit], None]
    read_file: Callable[[str], Awaitable[ToolResponse]]
    get_skill_registry: Callable[[], Any]


def _json_tool_response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, indent=2),
            )
        ]
    )


async def build_master_toolkit(
    session: dict[str, Any],
    runtime: MasterAgentRuntime,
) -> Toolkit:
    """Build a per-session toolkit with guarded, stateful workflow tools."""
    toolkit = Toolkit()
    toolkit.create_tool_group(
        "context",
        description="Skill discovery, deterministic Skill routing and RAG retrieval.",
        active=False,
        notes="Use for inspecting available Skills, selecting candidate Skills, and retrieving knowledge evidence.",
    )
    toolkit.create_tool_group(
        "planning",
        description="Requirement extraction and missing-field checks.",
        active=False,
        notes="Use for maintenance-plan requirement extraction and follow-up questions.",
    )
    toolkit.create_tool_group(
        "generation",
        description="Maintenance-plan evidence preparation and DOCX generation.",
        active=False,
        notes="Use only after required maintenance fields are complete.",
    )
    toolkit.create_tool_group(
        "document",
        description="Generated document inspection helpers.",
        active=False,
        notes="Use after a document has been generated or when the user asks about the generated file.",
    )

    toolkit.register_tool_function(runtime.read_file)

    def get_session_snapshot() -> ToolResponse:
        """Return current session state, generated document info and recent history."""
        state = session.setdefault("state", default_form_state())
        return _json_tool_response(
            {
                "status": "ok",
                "collected": state,
                "generated": session.get("generated"),
                "orchestration_ready": bool(session.get("orchestration")),
                "recent_history": session.get("history", [])[-6:],
            }
        )

    def list_registered_skills(filter_text: str = "") -> ToolResponse:
        """List registered AgentScope Skills.

        Args:
            filter_text: Optional keyword used to filter skill name or description.
        """
        keyword = filter_text.strip().lower()
        skills = []
        for skill in runtime.get_skill_registry().skills:
            haystack = f"{skill.name}\n{skill.description}".lower()
            if keyword and keyword not in haystack:
                continue
            skills.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "skill_dir": str(skill.path),
                    "skill_file": str(skill.path / "SKILL.md"),
                }
            )
        return _json_tool_response({"status": "ok", "skills": skills, "count": len(skills)})

    def select_maintenance_skills(
        maintenance_type: str = "",
        requirement_text: str = "",
    ) -> ToolResponse:
        """Select likely maintenance Skills using project routing rules.

        Args:
            maintenance_type: Maintenance type or action category.
            requirement_text: User requirement text or extracted resource details.
        """
        selected = runtime.get_skill_registry().select_skills(
            maintenance_type,
            requirement_text,
        )
        return _json_tool_response(
            {
                "status": "ok",
                "selected_skills": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "skill_dir": str(skill.path),
                        "skill_file": str(skill.path / "SKILL.md"),
                    }
                    for skill in selected
                ],
            }
        )

    async def retrieve_knowledge(query: str, top_k: int = 5) -> ToolResponse:
        """Retrieve remote RAG knowledge chunks from the configured knowledge base.

        Args:
            query: Retrieval query.
            top_k: Maximum number of chunks to return.
        """
        knowledge_base = get_knowledge_base(SKILLS_ROOT)
        if knowledge_base is None:
            return _json_tool_response(
                {
                    "status": "disabled",
                    "chunks": [],
                    "message": "RAG 未启用或百炼知识库配置不完整。",
                }
            )
        chunks = await knowledge_base.retrieve(query, top_k=top_k)
        return _json_tool_response(
            {
                "status": "ok",
                "query": query,
                "chunks": chunks,
                "count": len(chunks),
            }
        )

    async def update_requirements(message: str) -> ToolResponse:
        """Extract maintenance-plan requirement fields from a user message.

        Args:
            message: Latest user message that may contain maintenance-plan
                requirements or corrections.
        """
        state = session.setdefault("state", default_form_state())
        extracted = await extract_chat_updates(state, message)
        merge_updates(state, extracted.get("updates", {}))
        return _json_tool_response(
            {
                "status": "updated",
                "updates": extracted.get("updates", {}),
                "assistant_note": extracted.get("assistant_note", ""),
                "collected": state,
            }
        )

    def check_missing_requirements() -> ToolResponse:
        """Check whether the current session has all required plan fields."""
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        return _json_tool_response(
            {
                "status": "need_more" if missing else "complete",
                "missing_fields": missing,
                "question": build_missing_question(missing),
                "collected": state,
            }
        )

    async def prepare_plan_context() -> ToolResponse:
        """Select candidate Skills and retrieve RAG evidence for generation."""
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        if missing:
            return _json_tool_response(
                {
                    "status": "need_more",
                    "missing_fields": missing,
                    "question": build_missing_question(missing),
                    "collected": state,
                }
            )
        orchestration = await build_generation_orchestration_context(state)
        session["orchestration"] = orchestration
        return _json_tool_response(
            {
                "status": "ready",
                "selected_skills": orchestration["selected_skill_names"],
                "rag_enabled": orchestration["rag_enabled"],
                "rag_chunks_count": orchestration["rag_chunks_count"],
            }
        )

    async def generate_maintenance_plan(edit_instruction: str = "") -> ToolResponse:
        """Generate a maintenance-plan DOCX from the current collected state.

        Args:
            edit_instruction: Optional user instruction when revising an
                existing plan. Leave empty for new generation.
        """
        state = session.setdefault("state", default_form_state())
        missing = find_missing_fields(state)
        if missing:
            return _json_tool_response(
                {
                    "status": "need_more",
                    "missing_fields": missing,
                    "question": build_missing_question(missing),
                    "collected": state,
                }
            )
        orchestration = session.get("orchestration")
        if not orchestration:
            orchestration = await build_generation_orchestration_context(state)
            session["orchestration"] = orchestration
        file_id, _path, filename = await generate_docx_from_state(
            state,
            orchestration=orchestration,
            edit_instruction=edit_instruction,
        )
        generated = {
            "file_id": file_id,
            "filename": filename,
            "download_url": f"/api/download/{file_id}",
        }
        session["generated"] = generated
        return _json_tool_response(
            {
                "status": "generated",
                **generated,
                "collected": state,
                "evidence": {
                    "selected_skills": orchestration["selected_skill_names"],
                    "rag_enabled": orchestration["rag_enabled"],
                    "rag_chunks_count": orchestration["rag_chunks_count"],
                },
            }
        )

    def get_generated_document_info(include_preview: bool = False) -> ToolResponse:
        """Return metadata and optional text preview for the latest generated DOCX.

        Args:
            include_preview: Whether to include a Markdown text preview of the
                latest generated document.
        """
        generated = session.get("generated")
        if not generated:
            return _json_tool_response(
                {"status": "not_found", "message": "当前会话还没有生成文档。"}
            )
        path = get_generated_file(generated.get("file_id"))
        payload: dict[str, Any] = {
            "status": "ok" if path else "expired",
            "generated": generated,
            "exists": bool(path),
        }
        if path:
            payload["path"] = str(path)
            payload["size_bytes"] = path.stat().st_size
            if include_preview:
                payload["preview_markdown"] = docx_to_markdown(path.read_bytes())[:8000]
        return _json_tool_response(payload)

    toolkit.register_tool_function(get_session_snapshot, func_name="get_session_snapshot")
    toolkit.register_tool_function(
        list_registered_skills,
        group_name="context",
        func_name="list_registered_skills",
    )
    toolkit.register_tool_function(
        select_maintenance_skills,
        group_name="context",
        func_name="select_maintenance_skills",
    )
    toolkit.register_tool_function(
        retrieve_knowledge,
        group_name="context",
        func_name="retrieve_knowledge",
    )
    toolkit.register_tool_function(
        update_requirements,
        group_name="planning",
        func_name="update_requirements",
    )
    toolkit.register_tool_function(
        check_missing_requirements,
        group_name="planning",
        func_name="check_missing_requirements",
    )
    toolkit.register_tool_function(
        prepare_plan_context,
        group_name="generation",
        func_name="prepare_plan_context",
    )
    toolkit.register_tool_function(
        generate_maintenance_plan,
        group_name="generation",
        func_name="generate_maintenance_plan",
    )
    toolkit.register_tool_function(
        get_generated_document_info,
        group_name="document",
        func_name="get_generated_document_info",
    )
    toolkit.register_tool_function(toolkit.reset_equipped_tools)
    runtime.register_skills(toolkit)
    return toolkit


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
- 检修方案相关任务必须先用工具更新/检查需求字段。
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
