"""
国网云平台检修方案生成服务 - 基于 AgentScope 框架。

流程：用户输入 → Skill 路由/展开 → AnthropicChatModel → 结构化 JSON → generate_plan.py → Word 文档

Skill 装卸：修改 backend/skills/ 目录下的 Skill，重启即生效。
"""

import json
import os
import tempfile
import uuid
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
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
    response = await get_model()(
        [
            {"role": "system", "content": "你只输出可解析 JSON。"},
            {"role": "user", "content": prompt},
        ],
    )
    data = extract_json(get_response_text(response))
    if not isinstance(data, dict):
        data = {"updates": {}}

    inferred_updates = infer_updates_from_text(user_message)
    model_updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    data["updates"] = {**inferred_updates, **model_updates}
    if not data.get("assistant_note") and data["updates"]:
        data["assistant_note"] = "已收到需求描述，我先整理出可识别的信息。"
    return data


async def generate_docx_from_state(state: dict[str, str]) -> tuple[str, Path, str]:
    user_prompt = build_user_prompt(**state)
    response = await run_plan_agent(user_prompt)
    text = get_response_text(response)
    data = extract_json(text)

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
        data = extract_json(text)

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
