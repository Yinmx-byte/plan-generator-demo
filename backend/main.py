"""
国网云平台检修方案生成服务 - 基于 AgentScope 框架。

流程：用户输入 → workflow agent 意图识别 → plan agent 读取 Skill/RAG → Word 文档

Skill 装卸：修改 backend/skills/ 目录下的 Skill，重启即生效。
"""

import json
import os
import uuid
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rag import get_knowledge_base
from api.admin_routes import router as admin_router
from agents.workflow_agent import (
    WorkflowAgentRuntime,
    run_workflow_turn,
)
from agents.master_agent import (
    MasterAgentRuntime,
    run_master_agent_turn,
)
from runtime import (
    ROOT,
    SKILLS_ROOT,
    close_mcp_clients,
    get_formatter,
    get_model,
    get_skill_registry,
    get_skill_toolkit,
    get_toolkit,
    load_mcp_server_configs,
)
from services.json_utils import extract_json, get_response_text
from services.plan_generation import (
    build_generation_orchestration_context,
    docx_to_markdown,
    generate_docx_from_state,
    get_generated_file,
    get_generated_path,
)
from services.page_agent import run_page_agent_task, run_plan_validation_agent
from services.requirements import (
    build_missing_question,
    default_form_state,
    extract_chat_updates,
    find_missing_fields,
    merge_updates,
)

# ── Skill 注册 ──────────────────────────────────────────────────
_rag_enabled: bool = False
_chat_sessions: dict[str, dict[str, Any]] = {}
class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    execute_validation: bool = False


class ChatResetRequest(BaseModel):
    session_id: str


class PageAgentTaskRequest(BaseModel):
    task: str


class PlanTestRequest(BaseModel):
    message: str = ""
    state: Optional[dict[str, Any]] = None
    allow_partial: bool = False


def get_workflow_agent_runtime() -> WorkflowAgentRuntime:
    """Wire runtime dependencies for the workflow/controller agent."""
    return WorkflowAgentRuntime(
        get_model=get_model,
        get_formatter=get_formatter,
        get_toolkit=get_skill_toolkit,
        extract_json=extract_json,
        get_response_text=get_response_text,
    )


def get_master_agent_runtime() -> MasterAgentRuntime:
    """Wire runtime dependencies for the experimental master ReActAgent."""

    def register_project_skills(toolkit) -> None:
        for skill in get_skill_registry().skills:
            toolkit.register_agent_skill(str(skill.path))

    from runtime import read_file

    return MasterAgentRuntime(
        get_model=get_model,
        get_formatter=get_formatter,
        get_response_text=get_response_text,
        register_skills=register_project_skills,
        read_file=read_file,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _rag_enabled
    registry = get_skill_registry()
    print(f"[AgentScope] 已注册 Skills: {[skill.name for skill in registry.skills]}")
    _rag_enabled = get_knowledge_base(SKILLS_ROOT) is not None
    print(f"[AgentScope] RAG enabled: {_rag_enabled}")
    print(f"[AgentScope] MCP servers configured: {[item.get('name') for item in load_mcp_server_configs()]}")
    try:
        yield
    finally:
        await close_mcp_clients()


# ── FastAPI app ─────────────────────────────────────────────────
app = FastAPI(title="检修方案生成器", version="0.4.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "skills_loaded": [skill.name for skill in get_skill_registry().skills],
        "framework": "agentscope",
        "rag_enabled": _rag_enabled,
        "mcp_servers_configured": len(load_mcp_server_configs()),
        "model_provider": os.getenv("MODEL_PROVIDER", "deepseek"),
        "model_name": os.getenv("MODEL_NAME", "deepseek-v4-pro"),
    }


@app.get("/api/mcp")
async def list_mcp_servers():
    servers = []
    for item in load_mcp_server_configs():
        servers.append(
            {
                "name": item.get("name"),
                "type": item.get("type", "http_stateless"),
                "transport": item.get("transport"),
                "url": item.get("url"),
                "command": item.get("command"),
                "args": item.get("args"),
                "cwd": item.get("cwd"),
                "group_name": item.get("group_name", "mcp"),
                "enable_funcs": item.get("enable_funcs"),
                "disable_funcs": item.get("disable_funcs"),
            }
        )
    return {"servers": servers, "count": len(servers)}


@app.post("/api/mcp/start")
async def start_mcp_servers():
    await get_toolkit()
    return {
        "status": "ok",
        "message": "MCP 已启动或已处于可用状态。",
        "servers": [item.get("name") for item in load_mcp_server_configs()],
    }


@app.post("/api/page-agent/task")
async def execute_page_agent_task(request: PageAgentTaskRequest):
    return {
        "status": "ok",
        "result": await run_page_agent_task(request.task),
    }


app.include_router(admin_router)


@app.post("/api/dev/plan-test")
async def dev_plan_test(request: PlanTestRequest):
    """Development-only quick path: requirement text/state -> DOCX."""
    state = default_form_state()
    if request.state:
        merge_updates(state, request.state)

    if request.message.strip():
        extracted = await extract_chat_updates(state, request.message)
        merge_updates(state, extracted.get("updates", {}))
    else:
        extracted = {"updates": {}, "assistant_note": ""}

    missing = find_missing_fields(state)
    if missing and not request.allow_partial:
        return {
            "status": "need_more",
            "message": build_missing_question(missing),
            "missing_fields": missing,
            "collected": state,
            "extracted": extracted.get("updates", {}),
        }

    orchestration = await build_generation_orchestration_context(state)
    file_id, output_path, filename = await generate_docx_from_state(
        state,
        orchestration=orchestration,
    )
    return {
        "status": "generated",
        "file_id": file_id,
        "filename": filename,
        "download_url": f"/api/download/{file_id}",
        "output_path": str(output_path),
        "collected": state,
        "extracted": extracted.get("updates", {}),
        "evidence": {
            "selected_skills": orchestration["selected_skill_names"],
            "rag_enabled": orchestration["rag_enabled"],
            "rag_chunks_count": orchestration["rag_chunks_count"],
        },
    }


@app.post("/api/chat")
async def chat(request: ChatRequest):
    session_id = request.session_id or uuid.uuid4().hex
    session = _chat_sessions.setdefault(
        session_id,
        {
            "state": default_form_state(),
            "history": [],
            "generated": None,
        },
    )
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="请输入需求描述或补充信息")

    session["history"].append({"role": "user", "content": message})
    classification = await run_workflow_turn(message, session, get_workflow_agent_runtime())
    intent = classification["intent"]
    if intent == "chat":
        assistant_message = classification.get("assistant_message") or "我在。"
        session["history"].append({"role": "assistant", "content": assistant_message})
        return {
            "session_id": session_id,
            "status": "chat",
            "message": assistant_message,
            "intent": classification,
            "collected": session["state"],
        }
    extracted = {}
    if classification.get("should_extract", True):
        extracted = await extract_chat_updates(session["state"], message)
        merge_updates(session["state"], extracted.get("updates", {}))
    missing = find_missing_fields(session["state"])

    if missing and intent != "edit":
        assistant_message = extracted.get("assistant_note") or "已收到，我先整理这些信息。"
        assistant_message = f"{assistant_message}\n\n{build_missing_question(missing)}"
        session["history"].append({"role": "assistant", "content": assistant_message})
        return {
            "session_id": session_id,
            "status": "need_more",
            "message": assistant_message,
            "missing_fields": missing,
            "collected": session["state"],
        }

    orchestration = await build_generation_orchestration_context(session["state"])
    previous_document_text = ""
    if intent == "edit":
        generated_path = get_generated_path(session)
        if generated_path:
            previous_document_text = docx_path_to_markdown(generated_path)
    file_id, _path, filename = await generate_docx_from_state(
        session["state"],
        orchestration=orchestration,
        edit_instruction=message if intent == "edit" else "",
        previous_document_text=previous_document_text,
    )
    download_url = f"/api/download/{file_id}"
    assistant_message = "关键信息已收集完整，检修方案已生成。"
    if intent == "edit":
        assistant_message = "已基于上一版文档生成修订版。"
    session["generated"] = {
        "file_id": file_id,
        "filename": filename,
        "download_url": download_url,
    }
    validation_result = None
    if request.execute_validation:
        validation_result = await run_plan_validation_agent(
            session["state"],
            filename,
            download_url,
        )
    session["history"].append({"role": "assistant", "content": assistant_message})
    return {
        "session_id": session_id,
        "status": "generated",
        "message": assistant_message,
        "download_url": download_url,
        "filename": filename,
        "validation_result": validation_result,
        "collected": session["state"],
        "evidence": {
            "selected_skills": orchestration["selected_skill_names"],
            "rag_enabled": orchestration["rag_enabled"],
            "rag_chunks_count": orchestration["rag_chunks_count"],
        },
    }


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def docx_path_to_markdown(path: Path) -> str:
    return docx_to_markdown(path.read_bytes())


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    async def stream():
        session_id = request.session_id or uuid.uuid4().hex
        session = _chat_sessions.setdefault(
            session_id,
            {
                "state": default_form_state(),
                "history": [],
                "generated": None,
            },
        )
        message = request.message.strip()
        if not message:
            yield sse_event(
                "error",
                {"session_id": session_id, "message": "请输入需求描述或补充信息"},
            )
            return

        try:
            yield sse_event(
                "status",
                {"session_id": session_id, "message": "正在识别用户意图..."},
            )
            session["history"].append({"role": "user", "content": message})
            classification = await run_workflow_turn(message, session, get_workflow_agent_runtime())
            intent = classification["intent"]
            yield sse_event(
                "intent",
                {
                    "session_id": session_id,
                    "intent": intent,
                    "reason": classification.get("reason", ""),
                },
            )
            if intent == "chat":
                assistant_message = classification.get("assistant_message") or "我在。"
                session["history"].append({"role": "assistant", "content": assistant_message})
                yield sse_event(
                    "done",
                    {
                        "session_id": session_id,
                        "status": "chat",
                        "message": assistant_message,
                        "collected": session["state"],
                        "intent": classification,
                    },
                )
                return
            if intent == "edit":
                yield sse_event(
                    "status",
                    {"session_id": session_id, "message": "识别到这是对上一版文档的修订请求，正在读取已生成文档..."},
                )
            elif intent == "regenerate":
                yield sse_event(
                    "status",
                    {"session_id": session_id, "message": "识别到这是重新生成请求，将复用当前已收集需求，不读取上一版文档。"},
                )
            else:
                yield sse_event(
                    "status",
                    {"session_id": session_id, "message": "识别到检修方案生成需求，正在抽取关键信息..."},
                )
            extracted = {}
            if classification.get("should_extract", True):
                extracted = await extract_chat_updates(session["state"], message)
                merge_updates(session["state"], extracted.get("updates", {}))

            yield sse_event(
                "collected",
                {
                    "session_id": session_id,
                    "message": "关键信息抽取完成，正在检查是否需要补充。",
                    "collected": session["state"],
                },
            )
            missing = find_missing_fields(session["state"])

            if missing and intent != "edit":
                assistant_message = extracted.get("assistant_note") or "已收到，我先整理这些信息。"
                assistant_message = f"{assistant_message}\n\n{build_missing_question(missing)}"
                session["history"].append({"role": "assistant", "content": assistant_message})
                yield sse_event(
                    "done",
                    {
                        "session_id": session_id,
                        "status": "need_more",
                        "message": assistant_message,
                        "missing_fields": missing,
                        "collected": session["state"],
                    },
                )
                return

            yield sse_event(
                "status",
                {"session_id": session_id, "message": "信息已完整，正在初筛 Skill 并检索 RAG 参考资料..."},
            )
            orchestration = await build_generation_orchestration_context(session["state"])
            yield sse_event(
                "evidence",
                {
                    "session_id": session_id,
                    "message": "生成依据已准备完成，正在调用 AgentScope Skill 生成检修方案。",
                    "selected_skills": orchestration["selected_skill_names"],
                    "rag_enabled": orchestration["rag_enabled"],
                    "rag_chunks_count": orchestration["rag_chunks_count"],
                },
            )
            previous_document_text = ""
            if intent == "edit":
                generated_path = get_generated_path(session)
                if generated_path:
                    previous_document_text = docx_path_to_markdown(generated_path)

            trace_queue: asyncio.Queue = asyncio.Queue()

            async def trace_callback(trace_message: str) -> None:
                await trace_queue.put({"session_id": session_id, "message": trace_message})

            generation_task = asyncio.create_task(
                generate_docx_from_state(
                    session["state"],
                    orchestration=orchestration,
                    trace_callback=trace_callback,
                    edit_instruction=message if intent == "edit" else "",
                    previous_document_text=previous_document_text,
                )
            )
            while True:
                if generation_task.done() and trace_queue.empty():
                    break
                try:
                    trace_data = await asyncio.wait_for(trace_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield sse_event("trace", trace_data)

            file_id, _path, filename = await generation_task
            download_url = f"/api/download/{file_id}"
            assistant_message_override = "已基于上一版文档生成修订版。" if intent == "edit" else None
            assistant_message = "关键信息已收集完整，检修方案已生成。"
            if assistant_message_override:
                assistant_message = assistant_message_override
            session["generated"] = {
                "file_id": file_id,
                "filename": filename,
                "download_url": download_url,
            }
            session["history"].append({"role": "assistant", "content": assistant_message})
            validation_result = None
            if request.execute_validation:
                yield sse_event(
                    "status",
                    {
                        "session_id": session_id,
                        "message": "已生成检修方案，正在通过 Page Agent MCP 执行浏览器侧验证...",
                    },
                )
                validation_result = await run_plan_validation_agent(
                    session["state"],
                    filename,
                    download_url,
                )
            yield sse_event(
                "done",
                {
                    "session_id": session_id,
                    "status": "generated",
                    "message": assistant_message,
                    "download_url": download_url,
                    "filename": filename,
                    "validation_result": validation_result,
                    "collected": session["state"],
                    "evidence": {
                        "selected_skills": orchestration["selected_skill_names"],
                        "rag_enabled": orchestration["rag_enabled"],
                        "rag_chunks_count": orchestration["rag_chunks_count"],
                    },
                },
            )
        except HTTPException as exc:
            yield sse_event(
                "error",
                {"session_id": session_id, "message": exc.detail, "status_code": exc.status_code},
            )
        except Exception as exc:
            yield sse_event(
                "error",
                {"session_id": session_id, "message": f"生成失败：{exc}"},
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/stream")
async def master_agent_stream(request: ChatRequest):
    """Experimental autonomous Master ReActAgent entry.

    This does not replace /api/chat/stream. It is used to validate whether a
    single ReActAgent can plan and execute the maintenance workflow with
    guarded tools.
    """

    async def stream():
        session_id = request.session_id or uuid.uuid4().hex
        session = _chat_sessions.setdefault(
            session_id,
            {
                "state": default_form_state(),
                "history": [],
                "generated": None,
            },
        )
        message = request.message.strip()
        if not message:
            yield sse_event(
                "error",
                {"session_id": session_id, "message": "请输入需求描述或问题"},
            )
            return

        try:
            yield sse_event(
                "status",
                {
                    "session_id": session_id,
                    "message": "正在启动 Master ReActAgent 自主规划实验链路...",
                },
            )
            trace_queue: asyncio.Queue = asyncio.Queue()

            async def trace_callback(trace_message: str) -> None:
                await trace_queue.put({"session_id": session_id, "message": trace_message})

            task = asyncio.create_task(
                run_master_agent_turn(
                    message,
                    session,
                    get_master_agent_runtime(),
                    trace_callback=trace_callback,
                )
            )
            while True:
                if task.done() and trace_queue.empty():
                    break
                try:
                    trace_data = await asyncio.wait_for(trace_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield sse_event("trace", trace_data)

            response = await task
            assistant_message = get_response_text(response) or "Master Agent 已完成本轮处理。"
            generated = session.get("generated")
            yield sse_event(
                "done",
                {
                    "session_id": session_id,
                    "status": "generated" if generated else "done",
                    "message": assistant_message,
                    "generated": generated,
                    "collected": session["state"],
                },
            )
        except Exception as exc:
            yield sse_event(
                "error",
                {"session_id": session_id, "message": f"Master Agent 执行失败：{exc}"},
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat/reset")
async def reset_chat(request: ChatResetRequest):
    _chat_sessions.pop(request.session_id, None)
    return {"status": "ok"}


@app.get("/api/download/{file_id}")
async def download_generated(file_id: str):
    path = get_generated_file(file_id)
    if path is None:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ── 前端静态文件 ─────────────────────────────────────────────────
frontend_dir = ROOT.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
