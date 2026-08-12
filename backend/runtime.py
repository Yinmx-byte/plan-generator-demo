"""Shared AgentScope runtime wiring for the backend."""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from agentscope.formatter import AnthropicChatFormatter, OpenAIChatFormatter
from agentscope.message import TextBlock
from agentscope.mcp import HttpStatefulClient, HttpStatelessClient, StdIOStatefulClient
from agentscope.model import AnthropicChatModel, OpenAIChatModel
from agentscope.tool import Toolkit, ToolResponse
from dotenv import load_dotenv

from rag import get_knowledge_base, reset_knowledge_base
from scripts.generate_plan import build_document
from skills_runtime import SkillRegistry

ROOT = Path(__file__).parent
SKILLS_SEED_ROOT = ROOT / "skills"
# Downloaded Skill packages are a runtime cache.  Keep the default cache outside
# ``backend`` so Uvicorn's ``--reload`` watcher does not restart on a sync.
SKILLS_ROOT = Path(os.getenv("SKILLS_RUNTIME_ROOT", ROOT.parent / ".runtime_skills"))

load_dotenv(ROOT / ".env", override=True)

_skill_registry: Optional[SkillRegistry] = None
_skill_toolkit: Optional[Toolkit] = None
_toolkit: Optional[Toolkit] = None
_mcp_clients: list[Any] = []
ChatModel = AnthropicChatModel | OpenAIChatModel
_models: dict[tuple[str, str, bool, int], ChatModel] = {}

def get_skill_registry() -> SkillRegistry:
    """Load Skill metadata without expanding all Skill bodies."""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry(SKILLS_ROOT)
    return _skill_registry


async def reset_skill_runtime() -> None:
    """Reload skill metadata/toolkit after skill files change."""
    global _skill_registry, _skill_toolkit, _toolkit
    has_mcp_clients = bool(_mcp_clients)
    _skill_registry = None
    _skill_toolkit = None
    _toolkit = None
    reset_knowledge_base()
    # Existing session agents keep serving their current turn, then rebuild
    # from external history before the next turn so edited Skills take effect.
    from services.chat_sessions import get_chat_session_store

    get_chat_session_store().invalidate_master_agents()
    if has_mcp_clients:
        try:
            await asyncio.wait_for(
                close_mcp_clients(),
                timeout=float(os.getenv("MCP_CLOSE_TIMEOUT", "6")),
            )
        except asyncio.TimeoutError:
            _mcp_clients.clear()


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
        raise ValueError("只能读取 Skill 运行时缓存目录下的文件。")
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    content = resolved.read_text(encoding="utf-8")
    return ToolResponse(content=[TextBlock(type="text", text=content)])


async def build_maintenance_document(
    document_json: Any,
    style_contract: Optional[dict[str, Any]] = None,
) -> ToolResponse:
    """校验并试渲染检修方案 JSON。

    Args:
        document_json: 方案 JSON 对象或 JSON 字符串，必须包含 document.sections。
        style_contract: 可选的格式契约对象；省略时使用通用 composer Skill 配置。
    """
    if isinstance(document_json, str):
        data = json.loads(document_json)
    elif isinstance(document_json, dict):
        data = document_json
    else:
        raise ValueError("document_json 必须是 JSON 字符串或对象。")

    document = data.get("document")
    if not isinstance(document, dict):
        raise ValueError("缺少 document 对象。")
    sections = document.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("document.sections 必须是非空数组。")
    required_sections = ["背景", "检修类型", "现场环境", "实施计划", "风险评估", "实施步骤", "回滚步骤"]
    actual_sections = {
        str(section.get("heading", "")).replace(" ", "")
        for section in sections
        if isinstance(section, dict)
    }
    missing_sections = [
        name for name in required_sections if not any(name in actual for actual in actual_sections)
    ]
    if missing_sections:
        raise ValueError(f"缺少固定章节：{', '.join(missing_sections)}。")
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            raise ValueError(f"第 {index} 个 section 必须是对象。")
        if not section.get("heading"):
            raise ValueError(f"第 {index} 个 section 缺少 heading。")
        if not isinstance(section.get("blocks"), list) or not section.get("blocks"):
            raise ValueError(f"第 {index} 个 section 必须包含非空 blocks。")

    doc = build_document(data, style_contract)
    output_dir = Path(tempfile.gettempdir()) / "plan-generator" / "agent-preview"
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = output_dir / f"preview_{uuid.uuid4().hex[:8]}.docx"
    doc.save(str(preview_path))
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=(
                    "DOCX 渲染检查通过。"
                    f" sections={len(sections)} preview={preview_path}"
                    " 最终回答仍需输出完整 JSON 对象。"
                ),
            )
        ]
    )


def load_mcp_server_configs() -> list[dict[str, Any]]:
    config_path = ROOT / "mcp_servers.json"
    if not config_path.exists():
        return []
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("servers", [])
    if not isinstance(servers, list):
        raise RuntimeError("mcp_servers.json 中的 servers 必须是数组。")
    return servers


def resolve_backend_path(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return str(path.resolve())


def build_mcp_env(env_config: dict[str, Any] | None) -> dict[str, str] | None:
    if not env_config:
        return None
    env: dict[str, str] = {}
    for key, value in env_config.items():
        if isinstance(value, str) and value.startswith("$"):
            env[key] = os.getenv(value[1:], "")
        else:
            env[key] = str(value)
    return env


async def register_mcp_servers(toolkit: Toolkit) -> list[dict[str, Any]]:
    """Register configured MCP servers as AgentScope toolkit functions."""
    global _mcp_clients
    registered = []
    for item in load_mcp_server_configs():
        name = item.get("name")
        server_type = item.get("type", "http_stateless")
        if not name:
            raise RuntimeError("mcp_servers.json 中每个 server 都必须配置 name。")

        if server_type == "http_stateless":
            client = HttpStatelessClient(
                name=name,
                transport=item.get("transport", "streamable_http"),
                url=item["url"],
                headers=item.get("headers"),
                timeout=float(item.get("timeout", 30)),
                sse_read_timeout=float(item.get("sse_read_timeout", 300)),
            )
        elif server_type == "http_stateful":
            client = HttpStatefulClient(
                name=name,
                transport=item.get("transport", "streamable_http"),
                url=item["url"],
                headers=item.get("headers"),
                timeout=float(item.get("timeout", 30)),
                sse_read_timeout=float(item.get("sse_read_timeout", 300)),
            )
            await client.connect()
            _mcp_clients.append(client)
        elif server_type == "stdio":
            client = StdIOStatefulClient(
                name=name,
                command=item["command"],
                args=item.get("args"),
                env=build_mcp_env(item.get("env")),
                cwd=resolve_backend_path(item.get("cwd")),
            )
            await client.connect()
            _mcp_clients.append(client)
        else:
            raise RuntimeError(f"不支持的 MCP server type: {server_type}")

        group_name = item.get("group_name", "mcp")
        if group_name != "basic" and group_name not in toolkit.groups:
            toolkit.create_tool_group(
                group_name=group_name,
                description=f"MCP tools from {name}",
                active=True,
                notes="Use these tools only when the task needs external MCP capabilities.",
            )

        await toolkit.register_mcp_client(
            client,
            group_name=group_name,
            enable_funcs=item.get("enable_funcs"),
            disable_funcs=item.get("disable_funcs"),
            preset_kwargs_mapping=item.get("preset_kwargs_mapping"),
            namesake_strategy=item.get("namesake_strategy", "rename"),
            execution_timeout=item.get("execution_timeout"),
        )
        registered.append(
            {
                "name": name,
                "type": server_type,
                "group_name": group_name,
            }
        )
    return registered


async def close_mcp_clients() -> None:
    global _mcp_clients
    timeout = float(os.getenv("MCP_CLOSE_TIMEOUT", "6"))
    for client in _mcp_clients:
        close = getattr(client, "close", None)
        if close:
            try:
                await asyncio.wait_for(close(ignore_errors=True), timeout=timeout)
            except (asyncio.TimeoutError, Exception):
                pass
    _mcp_clients = []


async def get_toolkit() -> Toolkit:
    """Create the AgentScope Toolkit with tools, Skills and MCP clients."""
    global _toolkit
    if _toolkit is not None:
        return _toolkit

    toolkit = Toolkit()
    toolkit.register_tool_function(read_file)
    toolkit.register_tool_function(build_maintenance_document)
    for skill in get_skill_registry().skills:
        toolkit.register_agent_skill(str(skill.path))
    try:
        await asyncio.wait_for(
            register_mcp_servers(toolkit),
            timeout=float(os.getenv("MCP_CONNECT_TIMEOUT", "30")),
        )
    except Exception:
        await close_mcp_clients()
        raise
    _toolkit = toolkit
    return _toolkit


async def get_skill_toolkit() -> Toolkit:
    """Create a lightweight Toolkit for workflow routing and normal chat.

    This intentionally excludes MCP clients so intention recognition never
    starts browser/page-agent tools as a side effect.
    """
    global _skill_toolkit
    if _skill_toolkit is not None:
        return _skill_toolkit

    toolkit = Toolkit()
    toolkit.register_tool_function(read_file)
    toolkit.register_tool_function(build_maintenance_document)
    for skill in get_skill_registry().skills:
        toolkit.register_agent_skill(str(skill.path))
    _skill_toolkit = toolkit
    return _skill_toolkit


# ── LLM 客户端 ─────────────────────────────────────────────────
def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def role_model_name(env_name: str, deepseek_default: str) -> str:
    configured = os.getenv(env_name)
    if configured:
        return configured
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider == "deepseek":
        return deepseek_default
    return os.getenv("MODEL_NAME", deepseek_default)


def get_model_role_config() -> dict[str, Any]:
    """Return the effective model routing without creating API clients."""
    return {
        "master": role_model_name("MASTER_MODEL_NAME", "deepseek-v4-flash"),
        "extraction": role_model_name("EXTRACTION_MODEL_NAME", "deepseek-v4-flash"),
        "plan": role_model_name("PLAN_MODEL_NAME", "deepseek-v4-pro"),
        "plan_retry": role_model_name("PLAN_RETRY_MODEL_NAME", "deepseek-v4-pro"),
        "plan_retry_thinking": env_flag("PLAN_RETRY_THINKING_ENABLED", True),
    }


def get_role_model(
    model_name: str,
    *,
    thinking_enabled: bool = False,
    max_tokens_env: str = "MAX_TOKENS",
) -> ChatModel:
    """Create and cache one AgentScope model per role configuration."""
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    max_tokens = int(os.getenv(max_tokens_env, os.getenv("MAX_TOKENS", "16000")))
    cache_key = (provider, model_name, thinking_enabled, max_tokens)
    if cache_key in _models:
        return _models[cache_key]

    client_kwargs: dict[str, Any] = {}
    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("请在 .env 中设置 ANTHROPIC_API_KEY")
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        model: ChatModel = AnthropicChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=False,
            max_tokens=max_tokens,
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
        generate_kwargs: dict[str, Any] = {"max_tokens": max_tokens}
        if provider == "deepseek":
            generate_kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if thinking_enabled else "disabled"}
            }
        model = OpenAIChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=False,
            client_kwargs=client_kwargs if client_kwargs else None,
            generate_kwargs=generate_kwargs,
        )
    else:
        raise RuntimeError(f"不支持的 MODEL_PROVIDER: {provider}")

    _models[cache_key] = model
    return model


def get_master_model() -> ChatModel:
    return get_role_model(
        get_model_role_config()["master"],
        max_tokens_env="MASTER_MAX_TOKENS",
    )


def get_extraction_model() -> ChatModel:
    return get_role_model(
        get_model_role_config()["extraction"],
        max_tokens_env="EXTRACTION_MAX_TOKENS",
    )


def get_plan_model() -> ChatModel:
    return get_role_model(
        get_model_role_config()["plan"],
        max_tokens_env="PLAN_MAX_TOKENS",
    )


def get_plan_retry_model() -> ChatModel:
    config = get_model_role_config()
    return get_role_model(
        config["plan_retry"],
        thinking_enabled=config["plan_retry_thinking"],
        max_tokens_env="PLAN_RETRY_MAX_TOKENS",
    )


def get_formatter():
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider in {"openai", "deepseek"}:
        return OpenAIChatFormatter()
    return AnthropicChatFormatter()


def build_system_prompt() -> str:
    """Build the agent system prompt; Toolkit appends compact Skill prompts."""
    return f"""你是国网云平台检修方案生成 Agent。

遵循 AgentScope Skill 的渐进式披露原则：先根据 Skill 描述判断任务需要哪些 Skill，再通过 read_file 工具读取对应 SKILL.md。

如果用户消息中包含“编排上下文”，其中会说明 RAG 参考资料和 Skill 加载方式。Skill 选择由你根据 AgentScope 注册的 Skill 摘要自主完成；RAG 参考资料只作为模板、案例、API 约束和风险控制依据，不得覆盖 Skill 的硬性规则。

最终只输出 JSON，不要输出解释文字。JSON 顶层必须包含 document，document.sections 决定 Word 文档结构。"""


async def get_agent_knowledge():
    """Return AgentScope knowledge object when RAG is configured."""
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return None
    knowledge = await knowledge_base.get_knowledge()
    try:
        from agentscope.rag import KnowledgeBase
    except Exception:
        return None
    if isinstance(knowledge, KnowledgeBase):
        return knowledge
    if isinstance(knowledge, list) and all(isinstance(item, KnowledgeBase) for item in knowledge):
        return knowledge
    return None


