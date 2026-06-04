"""
国网云平台检修方案生成服务 - 基于 AgentScope 框架。

流程：用户输入 → Skill 路由/展开 → AnthropicChatModel → 结构化 JSON → generate_plan.py → Word 文档

Skill 装卸：修改 backend/skills/ 目录下的 Skill，重启即生效。
"""

import json
import os
import uuid
import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException
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
    evaluate: bool = False
    evaluate_generated_docx: bool = False
    reference_dir: Optional[str] = None


def get_workflow_agent_runtime() -> WorkflowAgentRuntime:
    """Wire runtime dependencies for the workflow/controller agent."""
    return WorkflowAgentRuntime(
        get_model=get_model,
        get_formatter=get_formatter,
        get_toolkit=get_skill_toolkit,
        extract_json=extract_json,
        get_response_text=get_response_text,
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


def run_iterator_evaluation(
    reference_dir: str,
    candidate_docx: Path,
    source_skill: Optional[Path] = None,
) -> dict[str, Any]:
    """Evaluate the generated DOCX and derive source Skill improvement hints."""
    script = Path.home() / ".codex" / "skills" / "maintenance-skill-iterator" / "scripts" / "evaluate_plan_quality.py"
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"未找到自迭代评估脚本：{script}")
    reference_path = Path(reference_dir)
    if not reference_path.exists():
        raise HTTPException(status_code=400, detail=f"参考文档目录不存在：{reference_dir}")
    command = [
        sys.executable,
        str(script),
        "--reference-dir",
        str(reference_path),
        "--candidate-docx",
        str(candidate_docx),
    ]
    if source_skill is not None:
        command.extend(["--source-skill", str(source_skill)])
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(os.getenv("SKILL_ITERATOR_TIMEOUT", "120")),
    )
    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "自迭代评估脚本执行失败",
                "stderr": process.stderr,
                "stdout": process.stdout,
            },
        )
    try:
        return json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "自迭代评估脚本输出不是合法 JSON",
                "stdout": process.stdout,
            },
        ) from exc


@app.post("/api/dev/plan-test")
async def dev_plan_test(request: PlanTestRequest):
    """Development-only quick path: requirement text/state -> DOCX -> optional quality evaluation."""
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
    generated_docx_evaluation = None
    should_evaluate_generated_docx = request.evaluate or request.evaluate_generated_docx
    if should_evaluate_generated_docx:
        if not request.reference_dir:
            raise HTTPException(status_code=400, detail="评估生成结果时必须提供 reference_dir")
        selected_skill_paths = {
            skill.name: skill.path for skill in get_skill_registry().skills
        }
        source_skill = None
        for skill_name in orchestration["selected_skill_names"]:
            if skill_name in selected_skill_paths and skill_name != "maintenance-plan-composer":
                source_skill = selected_skill_paths[skill_name] / "SKILL.md"
                break
        generated_docx_evaluation = await asyncio.to_thread(
            run_iterator_evaluation,
            request.reference_dir,
            output_path,
            source_skill,
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
        "generated_docx_evaluation": generated_docx_evaluation,
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


@app.post("/api/generate")
async def generate_plan(
    background: str = Form(default=""),
    maintenance_type: str = Form(default="配置变更"),
    network: str = Form(default="内网"),
    location: str = Form(default="国网亦庄数据中心二期运维专区"),
    instances: str = Form(default=""),
    schedule_year: str = Form(default=str(datetime.now().year) + "年"),
    schedule_start: str = Form(default=""),
    schedule_end: str = Form(default=""),
    provider: str = Form(default=""),
    executor: str = Form(default=""),
    reviewer: str = Form(default=""),
    security_officer: str = Form(default=""),
    ascm_account: str = Form(default=""),
    bastion_account: str = Form(default=""),
    ops_detail: str = Form(default=""),
    tech_params: str = Form(default=""),
):
    """接收检修需求，调用 Claude API 生成结构化数据，返回 .docx 文件。"""
    try:
        state = {
            "background": background,
            "maintenance_type": maintenance_type,
            "network": network,
            "location": location,
            "instances": instances,
            "schedule_year": schedule_year,
            "schedule_start": schedule_start,
            "schedule_end": schedule_end,
            "provider": provider,
            "executor": executor,
            "reviewer": reviewer,
            "security_officer": security_officer,
            "ascm_account": ascm_account,
            "bastion_account": bastion_account,
            "ops_detail": ops_detail,
            "tech_params": tech_params,
        }
        _file_id, output_path, filename = await generate_docx_from_state(state)
        return FileResponse(
            path=str(output_path),
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


# ── 前端静态文件 ─────────────────────────────────────────────────
frontend_dir = ROOT.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
