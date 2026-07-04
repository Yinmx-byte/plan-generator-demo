"""
国网云平台检修方案生成服务 - 基于 AgentScope 框架。

流程：用户输入 → Master ReActAgent 自主规划 → Skill/RAG 依据准备 → Word 文档

Skill 装卸：修改 backend/skills/ 目录下的 Skill，重启即生效。
"""

import json
import os
import uuid
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rag import get_knowledge_base
from api.admin_routes import router as admin_router
from api.cloud_routes import router as cloud_router
from agents.master_agent import (
    MasterAgentRuntime,
    get_simple_direct_reply,
    run_master_agent_turn,
)
from runtime import (
    ROOT,
    SKILLS_ROOT,
    close_mcp_clients,
    get_formatter,
    get_master_model,
    get_model_role_config,
    get_skill_registry,
    get_toolkit,
    load_mcp_server_configs,
)
from services.json_utils import get_response_text
from services.plan_generation import (
    build_generation_orchestration_context,
    generate_docx_from_state,
    get_generated_document,
    get_generated_file,
    render_docx,
    update_generated_document,
)
from services.page_agent import (
    run_page_agent_task,
    run_page_agent_task_events,
    run_plan_validation_agent,
)
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


class GeneratedDocumentUpdateRequest(BaseModel):
    data: dict[str, Any]


def get_master_agent_runtime() -> MasterAgentRuntime:
    """Wire runtime dependencies for the planner-first Master ReActAgent."""

    def register_project_skills(toolkit) -> None:
        for skill in get_skill_registry().skills:
            toolkit.register_agent_skill(str(skill.path))

    from runtime import read_file

    return MasterAgentRuntime(
        get_model=get_master_model,
        get_formatter=get_formatter,
        get_response_text=get_response_text,
        register_skills=register_project_skills,
        read_file=read_file,
        get_skill_registry=get_skill_registry,
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
    model_config = get_model_role_config()
    return {
        "status": "ok",
        "skills_loaded": [skill.name for skill in get_skill_registry().skills],
        "framework": "agentscope",
        "rag_enabled": _rag_enabled,
        "mcp_servers_configured": len(load_mcp_server_configs()),
        "model_provider": os.getenv("MODEL_PROVIDER", "deepseek"),
        "model_name": model_config["master"],
        "models": model_config,
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
app.include_router(cloud_router)


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
            "skill_selection_mode": orchestration.get("skill_selection_mode"),
            "registered_skills_count": len(get_skill_registry().skills),
            "rag_enabled": orchestration["rag_enabled"],
            "rag_chunks_count": orchestration["rag_chunks_count"],
            "rag_status": orchestration.get("rag_status"),
            "subject_anchor": orchestration.get("subject_anchor"),
            "rag_chunk_previews": orchestration.get("rag_chunk_previews", []),
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
        raise HTTPException(status_code=400, detail="请输入需求描述或问题")

    response = await run_master_agent_turn(
        message,
        session,
        get_master_agent_runtime(),
    )
    assistant_message = get_response_text(response) or "Master Agent 已完成本轮处理。"
    generated = session.get("generated")
    validation_result = None
    if request.execute_validation and generated:
        validation_result = await run_plan_validation_agent(
            session["state"],
            generated.get("filename", ""),
            generated.get("download_url", ""),
        )
    return {
        "session_id": session_id,
        "status": "generated" if generated else "done",
        "message": assistant_message,
        "generated": generated,
        "download_url": generated.get("download_url") if generated else None,
        "filename": generated.get("filename") if generated else None,
        "validation_result": validation_result,
        "collected": session["state"],
    }


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def describe_intent_hint(message: str) -> str:
    """Coarse UI hint only; routing is still handled by Master ReActAgent."""
    text = message.lower()
    if any(word in text for word in ("cpu", "内存", "磁盘", "资源组", "实例", "vpc", "ecs", "使用率", "资源占用", "有多少")):
        if not any(word in text for word in ("检修方案", "生成方案", "方案生成", "写一份", "出一份")):
            return "云资源问数"
    if any(word in text for word in ("page agent", "浏览器", "控制台", "验证文档", "执行验证")):
        return "浏览器验证"
    if any(word in text for word in ("检修方案", "生成方案", "方案生成", "修改方案", "重新生成", "检修窗口")):
        return "检修方案生成/修订"
    return "普通聊天或待模型进一步判断"


async def stream_page_agent_task(request: PageAgentTaskRequest):
    try:
        yield sse_event("status", {"message": "Page Agent 流式任务启动中..."})
        async for item in run_page_agent_task_events(request.task):
            item_type = item.get("type", "trace")
            if item_type == "status":
                yield sse_event("status", {"message": item.get("message", "")})
            elif item_type == "trace":
                yield sse_event(
                    "trace",
                    {
                        "message": item.get("message", ""),
                        "raw": item.get("raw"),
                    },
                )
            elif item_type == "done":
                yield sse_event("done", {"message": item.get("message", "")})
                return
            elif item_type == "error":
                yield sse_event("error", {"message": item.get("message", "")})
                return
    except Exception as exc:
        yield sse_event("error", {"message": f"Page Agent 流式执行失败：{exc}"})


@app.post("/api/page-agent/task/stream")
async def execute_page_agent_task_stream(request: PageAgentTaskRequest):
    return StreamingResponse(
        stream_page_agent_task(request),
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def stream_master_agent_response(request: ChatRequest, startup_message: str):
    """Shared SSE stream for the planner-first Master ReActAgent chain."""
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
    simple_reply = get_simple_direct_reply(message)
    if simple_reply:
        session.setdefault("history", [])
        session["history"].append({"role": "user", "content": message})
        session["history"].append({"role": "assistant", "content": simple_reply})
        yield sse_event(
            "done",
            {
                "session_id": session_id,
                "status": "done",
                "message": simple_reply,
                "generated": None,
                "download_url": None,
                "filename": None,
                "validation_result": None,
                "collected": session["state"],
            },
        )
        return

    agent_task: asyncio.Task | None = None
    try:
        yield sse_event(
            "status",
            {
                "session_id": session_id,
                "message": startup_message,
            },
        )
        yield sse_event(
            "trace",
            {
                "session_id": session_id,
                "message": (
                    "大步骤：意图识别\n"
                    f"初步意图：{describe_intent_hint(message)}。"
                    "后续由 Master ReActAgent 结合主控工作流 Skill 和工具调用继续确认。"
                ),
            },
        )
        yield sse_event(
            "trace",
            {
                "session_id": session_id,
                "message": "大步骤：自主规划\nMaster ReActAgent 会按需要调用需求抽取、Skill/RAG 准备、云资源查询、文档生成或浏览器验证工具。",
            },
        )
        trace_queue: asyncio.Queue = asyncio.Queue()

        async def trace_callback(trace_message: str) -> None:
            await trace_queue.put({"session_id": session_id, "message": trace_message})

        agent_task = asyncio.create_task(
            run_master_agent_turn(
                message,
                session,
                get_master_agent_runtime(),
                trace_callback=trace_callback,
            )
        )
        while True:
            if agent_task.done() and trace_queue.empty():
                break
            try:
                trace_data = await asyncio.wait_for(trace_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            yield sse_event("trace", trace_data)

        response = await agent_task
        assistant_message = get_response_text(response) or "Master Agent 已完成本轮处理。"
        generated = session.get("generated")
        validation_result = None
        if request.execute_validation and generated:
            yield sse_event(
                "status",
                {
                    "session_id": session_id,
                    "message": "已生成检修方案，正在通过 Page Agent MCP 执行浏览器侧验证...",
                },
            )
            validation_result = await run_plan_validation_agent(
                session["state"],
                generated.get("filename", ""),
                generated.get("download_url", ""),
            )
        yield sse_event(
            "done",
            {
                "session_id": session_id,
                "status": "generated" if generated else "done",
                "message": assistant_message,
                "generated": generated,
                "download_url": generated.get("download_url") if generated else None,
                "filename": generated.get("filename") if generated else None,
                "validation_result": validation_result,
                "collected": session["state"],
            },
        )
    except Exception as exc:
        yield sse_event(
            "error",
            {"session_id": session_id, "message": f"Master Agent 执行失败：{exc}"},
        )

    finally:
        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
            try:
                await asyncio.wait_for(agent_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass


@app.post("/api/chat/stream")
@app.post("/api/agent/stream")
async def master_agent_stream(request: ChatRequest):
    """Planner-first Master ReActAgent stream entry."""
    return StreamingResponse(
        stream_master_agent_response(
            request,
            "正在启动 Master ReActAgent 自主规划主链路...",
        ),
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


@app.get("/api/documents/{file_id}")
async def get_generated_document_preview(file_id: str):
    data = get_generated_document(file_id)
    if data is None:
        raise HTTPException(status_code=404, detail="生成文档数据不存在或已过期")
    return {"status": "ok", "file_id": file_id, "data": data}


@app.put("/api/documents/{file_id}")
async def save_generated_document(file_id: str, request: GeneratedDocumentUpdateRequest):
    data = update_generated_document(file_id, request.data)
    return {"status": "ok", "file_id": file_id, "data": data}


@app.post("/api/documents/{file_id}/render")
async def render_generated_document(file_id: str):
    data = get_generated_document(file_id)
    if data is None:
        raise HTTPException(status_code=404, detail="生成文档数据不存在或已过期")
    new_file_id, _path, filename = render_docx(data)
    return {
        "status": "generated",
        "file_id": new_file_id,
        "filename": filename,
        "download_url": f"/api/download/{new_file_id}",
    }


# ── 前端静态文件 ─────────────────────────────────────────────────
frontend_dir = ROOT.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
