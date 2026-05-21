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
from typing import Optional

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

from rag import get_knowledge_base
from skills_runtime import SkillRegistry
from scripts.generate_plan import build_document

load_dotenv()

ROOT = Path(__file__).parent
SKILLS_ROOT = ROOT / "skills"

# ── Skill 注册 ──────────────────────────────────────────────────
_skill_registry: Optional[SkillRegistry] = None
_toolkit: Optional[Toolkit] = None
_rag_enabled: bool = False


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
    return agent(Msg("user", user_prompt, "user"))


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


def get_response_text(response) -> str:
    """Extract text from AgentScope 1.x ChatResponse."""
    if hasattr(response, "get_text_content"):
        return response.get_text_content()
    if hasattr(response, "text"):
        return response.text
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    texts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return "\n".join(texts)


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
