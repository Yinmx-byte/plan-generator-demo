"""
国网云平台检修方案生成服务 - 基于 AgentScope 框架。

流程：用户输入 → Skill 路由/展开 → AnthropicChatModel → 结构化 JSON → generate_plan.py → Word 文档

Skill 装卸：修改 backend/skills/ 目录下的 Skill，重启即生效。
"""

import json
import os
import re
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agentscope.agent import ReActAgent
from agentscope.formatter import AnthropicChatFormatter, OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg, TextBlock
from agentscope.model import AnthropicChatModel, OpenAIChatModel
from agentscope.tool import Toolkit, ToolResponse
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rag import get_knowledge_base
from skills_runtime import SkillRegistry
ROOT = Path(__file__).parent
SKILLS_ROOT = ROOT / "skills"

from scripts.generate_plan import build_document

load_dotenv(ROOT / ".env")

# ── Skill 注册 ──────────────────────────────────────────────────
_skill_registry: Optional[SkillRegistry] = None
_toolkit: Optional[Toolkit] = None
_rag_enabled: bool = False
_chat_sessions: dict[str, dict[str, Any]] = {}
_generated_files: dict[str, Path] = {}

REQUIRED_FIELDS = {
    "background": "检修背景/检修事项",
    "maintenance_type": "检修类型",
    "network": "内外网环境",
    "location": "实施地点",
    "instances": "涉及的组件实例、组织、资源集",
    "schedule_start": "检修开始时间",
    "schedule_end": "检修结束时间",
    "provider": "方案提供人",
    "executor": "检修执行人",
    "reviewer": "检修复核人",
    "security_officer": "安全责任人",
    "ascm_account": "ASCM 授权账号",
    "bastion_account": "堡垒机账号",
}

FORM_FIELDS = [
    "background",
    "maintenance_type",
    "network",
    "location",
    "instances",
    "schedule_year",
    "schedule_start",
    "schedule_end",
    "provider",
    "executor",
    "reviewer",
    "security_officer",
    "ascm_account",
    "bastion_account",
    "ops_detail",
    "tech_params",
]


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ChatResetRequest(BaseModel):
    session_id: str


def get_skill_registry() -> SkillRegistry:
    """Load Skill metadata without expanding all Skill bodies."""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry(SKILLS_ROOT)
    return _skill_registry


def reset_skill_runtime() -> None:
    """Reload skill metadata/toolkit after skill files change."""
    global _skill_registry, _toolkit
    _skill_registry = None
    _toolkit = None


async def read_file(file_path: str) -> ToolResponse:
    """读取指定 Skill 文件内容。

    Args:
        file_path: 要读取的 Skill 文件路径，通常是某个 SKILL.md。
    """
    requested = Path(file_path)
    if not requested.is_absolute():
        requested = ROOT / requested
    resolved = requested.resolve()
    skills_root = SKILLS_ROOT.resolve()
    if not str(resolved).startswith(str(skills_root)):
        raise ValueError("只能读取 backend/skills 目录下的 Skill 文件。")
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    content = resolved.read_text(encoding="utf-8")
    return ToolResponse(content=[TextBlock(type="text", text=content)])


def get_toolkit() -> Toolkit:
    """Create the AgentScope Toolkit with tools and registered Skills."""
    global _toolkit
    if _toolkit is not None:
        return _toolkit

    toolkit = Toolkit()
    toolkit.register_tool_function(read_file)
    for skill in get_skill_registry().skills:
        toolkit.register_agent_skill(str(skill.path))
    _toolkit = toolkit
    return _toolkit


# ── LLM 客户端 (AgentScope AnthropicChatModel) ─────────────────
_model: Optional[AnthropicChatModel | OpenAIChatModel] = None


def get_model() -> AnthropicChatModel | OpenAIChatModel:
    global _model
    if _model is None:
        provider = os.getenv("MODEL_PROVIDER", "anthropic").lower()
        model_name = os.getenv("MODEL_NAME", "claude-sonnet-4-6")
        client_kwargs = {}

        if provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("请在 .env 中设置 ANTHROPIC_API_KEY")
            base_url = os.getenv("ANTHROPIC_BASE_URL")
            if base_url:
                client_kwargs["base_url"] = base_url
            _model = AnthropicChatModel(
                model_name=model_name,
                api_key=api_key,
                stream=False,
                max_tokens=int(os.getenv("MAX_TOKENS", "4096")),
                client_kwargs=client_kwargs if client_kwargs else None,
            )
        elif provider in {"openai", "deepseek"}:
            env_key = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
            api_key = os.getenv(env_key) or os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError(f"请在 .env 中设置 {env_key}")
            base_url = (
                os.getenv("DEEPSEEK_BASE_URL")
                if provider == "deepseek"
                else os.getenv("OPENAI_BASE_URL")
            )
            if base_url:
                client_kwargs["base_url"] = base_url
            _model = OpenAIChatModel(
                model_name=model_name,
                api_key=api_key,
                stream=False,
                client_kwargs=client_kwargs if client_kwargs else None,
                generate_kwargs={
                    "max_tokens": int(os.getenv("MAX_TOKENS", "4096")),
                    **(
                        {"extra_body": {"thinking": {"type": "disabled"}}}
                        if provider == "deepseek"
                        else {}
                    ),
                },
            )
        else:
            raise RuntimeError(f"不支持的 MODEL_PROVIDER: {provider}")
    return _model


def get_formatter():
    provider = os.getenv("MODEL_PROVIDER", "anthropic").lower()
    if provider in {"openai", "deepseek"}:
        return OpenAIChatFormatter()
    return AnthropicChatFormatter()


def build_system_prompt() -> str:
    """Build the agent system prompt; Toolkit appends compact Skill prompts."""
    return f"""你是国网云平台检修方案生成 Agent。

遵循 AgentScope Skill 的渐进式披露原则：先根据 Skill 描述判断任务需要哪些 Skill，再通过 read_file 工具读取对应 SKILL.md。

最终只输出 JSON，不要输出解释文字。JSON 顶层必须包含 document，document.sections 决定 Word 文档结构。"""


async def get_agent_knowledge():
    """Return AgentScope knowledge object when RAG is configured."""
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return None
    return await knowledge_base.get_knowledge()


def extract_json(text: str) -> dict:
    """从 LLM 返回的文本中提取 JSON。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise

        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            char = text[idx]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : idx + 1])
        raise


async def repair_and_extract_json(text: str) -> dict:
    """Repair slightly malformed model JSON and parse it again."""
    try:
        return extract_json(text)
    except json.JSONDecodeError:
        repair_prompt = f"""下面是一段模型输出的检修方案 JSON，但它可能存在漏逗号、尾随逗号、代码块包裹或混入说明文字等格式问题。
请在不改变语义和字段内容的前提下，把它修复为严格合法 JSON。

要求：
- 只输出 JSON 对象
- 不要输出 markdown
- 不要解释
- 保留 document、sections、tables、steps 等原有结构

原始输出：
{text}
"""
        response = await get_model()(
            [
                {"role": "system", "content": "你是 JSON 修复器，只输出严格合法 JSON。"},
                {"role": "user", "content": repair_prompt},
            ],
        )
        try:
            return extract_json(get_response_text(response))
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail="模型返回的方案 JSON 无法解析，请重试或补充更明确的需求。",
            ) from exc


def build_user_prompt(
    background: str, maintenance_type: str, network: str, location: str,
    instances: str, schedule_year: str, schedule_start: str, schedule_end: str,
    provider: str, executor: str, reviewer: str, security_officer: str,
    ascm_account: str, bastion_account: str, ops_detail: str, tech_params: str,
) -> str:
    """Convert form fields into the user task for the Agent."""
    return f"""请根据以下检修需求生成标准化检修方案 JSON。

你必须先根据系统中注册的 Agent Skills 判断需要哪些 Skill，然后使用 read_file 工具读取对应 SKILL.md。若涉及多个检修类型，应组合多个 Skill。

## 背景与检修事项
{background}

## 检修类型
{maintenance_type}

## 现场环境
内/外网环境：{network}
实施地点：{location}
涉及的组件实例描述：
{instances}

## 检修窗口
{schedule_year} {schedule_start} 至 {schedule_end}

## 人员信息
方案提供人：{provider}
检修执行人：{executor}
检修复核人：{reviewer}
安全责任人：{security_officer}

## 授权账号
ASCM账号：{ascm_account}
堡垒机账号：{bastion_account}

## 检修操作补充说明（可选）
{ops_detail}

注意：即使补充说明为空，也必须根据已激活 Skill 自动生成完整的“六、实施步骤”，不要要求用户手工提供详细操作步骤。

## 技术参数
{tech_params}
"""


async def run_plan_agent(user_prompt: str):
    """Run the native AgentScope ReActAgent for plan generation."""
    agent = ReActAgent(
        name="MaintenancePlanGenerator",
        sys_prompt=build_system_prompt(),
        model=get_model(),
        formatter=get_formatter(),
        toolkit=get_toolkit(),
        memory=InMemoryMemory(),
        knowledge=await get_agent_knowledge(),
        enable_rewrite_query=True,
        max_iters=int(os.getenv("AGENT_MAX_ITERS", "8")),
    )
    agent.set_console_output_enabled(False)
    return await agent(Msg("user", user_prompt, "user"))


# ── API 路由 ────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _rag_enabled
    registry = get_skill_registry()
    print(f"[AgentScope] 已注册 Skills: {[skill.name for skill in registry.skills]}")
    _rag_enabled = get_knowledge_base(SKILLS_ROOT) is not None
    print(f"[AgentScope] RAG enabled: {_rag_enabled}")
    yield


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
        "model_provider": os.getenv("MODEL_PROVIDER", "anthropic"),
        "model_name": os.getenv("MODEL_NAME", "claude-sonnet-4-6"),
    }


@app.get("/api/skills")
async def list_skills():
    registry = get_skill_registry()
    return {
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
            }
            for skill in registry.skills
        ]
    }


def safe_skill_dir_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip(".-")
    if not value:
        raise HTTPException(status_code=400, detail="Skill 名称不能为空")
    return value


def ensure_within_directory(base: Path, target: Path) -> None:
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    if not str(target_resolved).startswith(str(base_resolved)):
        raise HTTPException(status_code=400, detail="非法文件路径")


@app.post("/api/skills/upload")
async def upload_skill(
    file: UploadFile = File(...),
    skill_name: str = Form(default=""),
):
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")

    if suffix == ".zip":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path) as archive:
                skill_entries = [
                    name
                    for name in archive.namelist()
                    if not name.endswith("/") and Path(name).name == "SKILL.md"
                ]
                if not skill_entries:
                    raise HTTPException(status_code=400, detail="zip 中未找到 SKILL.md")
                skill_root_parts = Path(skill_entries[0]).parent.parts
                inferred_name = skill_root_parts[-1] if skill_root_parts else Path(filename).stem
                target_dir = SKILLS_ROOT / safe_skill_dir_name(skill_name or inferred_name)
                target_dir.mkdir(parents=True, exist_ok=True)
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    member_path = Path(member.filename)
                    if ".." in member_path.parts:
                        raise HTTPException(status_code=400, detail="zip 中包含非法路径")
                    parts = member_path.parts
                    if skill_root_parts and parts[: len(skill_root_parts)] == skill_root_parts:
                        parts = parts[len(skill_root_parts) :]
                    if not parts:
                        continue
                    output_path = target_dir.joinpath(*parts)
                    ensure_within_directory(target_dir, output_path)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(archive.read(member))
        finally:
            tmp_path.unlink(missing_ok=True)
    else:
        text = raw.decode("utf-8")
        if "name:" not in text and "# " not in text:
            raise HTTPException(status_code=400, detail="请上传有效的 SKILL.md")
        target_name = safe_skill_dir_name(skill_name or Path(filename).stem or "uploaded-skill")
        target_dir = SKILLS_ROOT / target_name
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "SKILL.md").write_text(text, encoding="utf-8")

    reset_skill_runtime()
    return {
        "status": "ok",
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "path": str(skill.path),
            }
            for skill in get_skill_registry().skills
        ],
    }


@app.get("/api/rag/retrieve")
async def retrieve_rag(query: str = Query(default="", description="检索问题")):
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return {
            "enabled": False,
            "chunks": [],
            "message": "RAG 未启用：请配置 OPENAI_API_KEY 或 EMBEDDING_API_KEY。",
        }
    return {
        "enabled": True,
        "chunks": await knowledge_base.retrieve(query),
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
    extracted = await extract_chat_updates(session["state"], message)
    merge_updates(session["state"], extracted.get("updates", {}))
    missing = find_missing_fields(session["state"])

    if missing:
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

    file_id, _path, filename = await generate_docx_from_state(session["state"])
    download_url = f"/api/download/{file_id}"
    assistant_message = "关键信息已收集完整，检修方案已生成。"
    session["generated"] = {
        "file_id": file_id,
        "filename": filename,
        "download_url": download_url,
    }
    session["history"].append({"role": "assistant", "content": assistant_message})
    return {
        "session_id": session_id,
        "status": "generated",
        "message": assistant_message,
        "download_url": download_url,
        "filename": filename,
        "collected": session["state"],
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
                {"session_id": session_id, "message": "正在抽取需求中的关键信息..."},
            )
            session["history"].append({"role": "user", "content": message})
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

            if missing:
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
                {"session_id": session_id, "message": "信息已完整，正在调用 Skill 生成检修方案..."},
            )
            file_id, _path, filename = await generate_docx_from_state(session["state"])
            download_url = f"/api/download/{file_id}"
            assistant_message = "关键信息已收集完整，检修方案已生成。"
            session["generated"] = {
                "file_id": file_id,
                "filename": filename,
                "download_url": download_url,
            }
            session["history"].append({"role": "assistant", "content": assistant_message})
            yield sse_event(
                "done",
                {
                    "session_id": session_id,
                    "status": "generated",
                    "message": assistant_message,
                    "download_url": download_url,
                    "filename": filename,
                    "collected": session["state"],
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
    path = _generated_files.get(file_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def get_response_text(response) -> str:
    """Extract text from AgentScope 1.x ChatResponse."""
    content = response
    for attr in ("get_text_content", "text", "content"):
        try:
            value = getattr(response, attr)
        except (AttributeError, KeyError):
            continue
        if callable(value):
            return value()
        if isinstance(value, str):
            return value
        if value is not None:
            content = value
            break

    if isinstance(content, str):
        return content
    texts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts)


def default_form_state() -> dict[str, str]:
    return {
        "background": "",
        "maintenance_type": "",
        "network": "",
        "location": "国网亦庄数据中心二期运维专区",
        "instances": "",
        "schedule_year": str(datetime.now().year) + "年",
        "schedule_start": "",
        "schedule_end": "",
        "provider": "",
        "executor": "",
        "reviewer": "",
        "security_officer": "",
        "ascm_account": "",
        "bastion_account": "",
        "ops_detail": "",
        "tech_params": "",
    }


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def merge_updates(state: dict[str, str], updates: dict[str, Any]) -> None:
    for key in FORM_FIELDS:
        value = normalize_value(updates.get(key))
        if value:
            state[key] = value


def find_missing_fields(state: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED_FIELDS if not state.get(key, "").strip()]


def build_missing_question(missing: list[str]) -> str:
    labels = [REQUIRED_FIELDS[key] for key in missing[:4]]
    if not labels:
        return ""
    return "还需要补充：" + "、".join(labels) + "。请直接回复这些信息即可。"


def infer_updates_from_text(user_message: str) -> dict[str, str]:
    """Capture obvious requirement clues before relying on free-form LLM output."""
    text = user_message.strip()
    lower_text = text.lower()
    updates: dict[str, str] = {}

    if text:
        updates["background"] = text

    def labeled_value(*labels: str, multiline: bool = False) -> str:
        label_group = "|".join(re.escape(label) for label in labels)
        if multiline:
            next_labels = (
                "检修背景|检修类型|网络环境|内外网环境|实施地点|涉及实例|检修窗口|"
                "方案提供人|检修执行人|检修复核人|安全责任人|ASCM 授权账号|"
                "ASCM授权账号|堡垒机账号|技术参数|补充要求"
            )
            pattern = rf"(?:{label_group})\s*[:：]\s*(.*?)(?=\n\s*(?:{next_labels})\s*[:：]|\Z)"
            match = re.search(pattern, text, re.S | re.I)
        else:
            match = re.search(rf"(?:{label_group})\s*[:：]\s*([^\n]+)", text, re.I)
        return match.group(1).strip() if match else ""

    labeled_background = labeled_value("检修背景", multiline=True)
    if labeled_background:
        updates["background"] = labeled_background

    label_map = {
        "maintenance_type": ("检修类型",),
        "network": ("网络环境", "内外网环境"),
        "location": ("实施地点",),
        "instances": ("涉及实例", "涉及的组件实例"),
        "provider": ("方案提供人",),
        "executor": ("检修执行人",),
        "reviewer": ("检修复核人",),
        "security_officer": ("安全责任人",),
        "ascm_account": ("ASCM 授权账号", "ASCM授权账号"),
        "bastion_account": ("堡垒机账号",),
    }
    for field, labels in label_map.items():
        value = labeled_value(*labels)
        if value:
            updates[field] = value

    tech_params = labeled_value("技术参数", multiline=True)
    if tech_params:
        updates["tech_params"] = tech_params
    ops_detail = labeled_value("补充要求", multiline=True)
    if ops_detail:
        updates["ops_detail"] = ops_detail

    schedule = labeled_value("检修窗口")
    if schedule:
        year_match = re.search(r"(\d{4}年)", schedule)
        if year_match:
            updates["schedule_year"] = year_match.group(1)
        parts = re.split(r"\s*(?:至|到|-|—|~)\s*", schedule, maxsplit=1)
        if len(parts) == 2:
            updates["schedule_start"] = parts[0].strip()
            updates["schedule_end"] = parts[1].strip()
        else:
            updates["schedule_start"] = schedule

    has_internal = "内网" in text
    has_external = "外网" in text
    if has_internal and has_external:
        updates["network"] = "内、外网"
    elif has_internal:
        updates["network"] = "内网"
    elif has_external:
        updates["network"] = "外网"

    if any(keyword in lower_text for keyword in ("ecs", "云服务器")) and any(
        keyword in text for keyword in ("创建", "新建", "申请", "开通")
    ):
        updates["maintenance_type"] = "配置变更"
        if not updates.get("instances"):
            updates["instances"] = text
    elif any(keyword in text for keyword in ("扩容", "缩容", "扩缩容")):
        updates["maintenance_type"] = "组件扩缩容"
    elif any(keyword in text for keyword in ("升级", "版本")):
        updates["maintenance_type"] = "组件升级"
    elif any(keyword in lower_text for keyword in ("数据库", "polardb", "mysql", "mongodb", "redis")):
        updates["maintenance_type"] = "数据库变更"

    return updates


async def extract_chat_updates(state: dict[str, str], user_message: str) -> dict[str, Any]:
    """Use the current LLM to update the structured requirement state."""
    prompt = f"""你是检修方案需求信息抽取助手。请从用户最新消息中抽取检修方案生成所需字段，并结合已有状态更新。

已有状态 JSON：
{json.dumps(state, ensure_ascii=False, indent=2)}

用户最新消息：
{user_message}

字段说明：
- background: 检修背景/检修事项，可多行
- maintenance_type: 配置变更/组件升级/组件扩缩容/数据库变更/日常维护（原硬件设备）/其他
- network: 内网/外网/内、外网
- location: 实施地点
- instances: 涉及实例，尽量包含事项名称、组织、资源集
- schedule_year/schedule_start/schedule_end: 检修窗口
- provider/executor/reviewer/security_officer: 人员信息
- ascm_account/bastion_account: 授权账号
- ops_detail: 用户额外约束或补充说明，不要把它当成详细步骤来源
- tech_params: 技术参数 JSON 或自然语言参数

只输出 JSON：
{{
  "updates": {{"field": "value"}},
  "assistant_note": "对已收到信息的简短确认，不超过60字"
}}

不要输出 markdown，不要解释。"""
    inferred_updates = infer_updates_from_text(user_message)
    try:
        response = await get_model()(
            [
                {"role": "system", "content": "你只输出可解析 JSON。"},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception:
        return {
            "updates": inferred_updates,
            "assistant_note": "已收到需求描述，我先整理出可识别的信息。",
        }
    try:
        data = extract_json(get_response_text(response))
    except json.JSONDecodeError:
        data = {"updates": {}, "assistant_note": "已收到需求描述，我先整理出可识别的信息。"}
    if not isinstance(data, dict):
        data = {"updates": {}}

    model_updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    data["updates"] = {**inferred_updates, **model_updates}
    if not data.get("assistant_note") and data["updates"]:
        data["assistant_note"] = "已收到需求描述，我先整理出可识别的信息。"
    return data


async def generate_docx_from_state(state: dict[str, str]) -> tuple[str, Path, str]:
    user_prompt = build_user_prompt(**state)
    response = await run_plan_agent(user_prompt)
    text = get_response_text(response)
    data = await repair_and_extract_json(text)

    data.setdefault("department", "云运营中心平台运维处")
    data.setdefault("date", datetime.now().strftime("%Y年%m月%d日"))

    doc = build_document(data)
    output_dir = Path(tempfile.gettempdir()) / "plan-generator"
    output_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    output_path = output_dir / f"检修方案_{file_id[:8]}.docx"
    doc.save(str(output_path))
    _generated_files[file_id] = output_path

    files = sorted(
        output_dir.glob("检修方案_*.docx"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old_file in files[10:]:
        try:
            old_file.unlink()
        except OSError:
            pass

    filename = data.get("title") or data.get("document", {}).get("title", "检修方案")
    return file_id, output_path, filename + ".docx"


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
        user_prompt = build_user_prompt(
            background=background, maintenance_type=maintenance_type,
            network=network, location=location, instances=instances,
            schedule_year=schedule_year, schedule_start=schedule_start,
            schedule_end=schedule_end, provider=provider, executor=executor,
            reviewer=reviewer, security_officer=security_officer,
            ascm_account=ascm_account, bastion_account=bastion_account,
            ops_detail=ops_detail, tech_params=tech_params,
        )

        response = await run_plan_agent(user_prompt)

        text = get_response_text(response)
        data = await repair_and_extract_json(text)

        data.setdefault("department", "云运营中心平台运维处")
        data.setdefault("date", datetime.now().strftime("%Y年%m月%d日"))

        doc = build_document(data)
        output_dir = Path(tempfile.gettempdir()) / "plan-generator"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"检修方案_{uuid.uuid4().hex[:8]}.docx"
        doc.save(str(output_path))

        # 清理旧文件
        files = sorted(
            output_dir.glob("检修方案_*.docx"),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        for old_file in files[10:]:
            try:
                old_file.unlink()
            except OSError:
                pass

        filename = data.get("title") or data.get("document", {}).get("title", "检修方案")
        filename += ".docx"
        return FileResponse(
            path=str(output_path),
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="LLM 返回的数据格式异常，请重试")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成失败: {str(e)}")


# ── 前端静态文件 ─────────────────────────────────────────────────
frontend_dir = ROOT.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")
