"""Shared AgentScope runtime wiring for the backend."""

import json
import os
from pathlib import Path
from typing import Any, Optional

from agentscope.formatter import AnthropicChatFormatter, OpenAIChatFormatter
from agentscope.message import TextBlock
from agentscope.mcp import HttpStatefulClient, HttpStatelessClient, StdIOStatefulClient
from agentscope.model import AnthropicChatModel, OpenAIChatModel
from agentscope.tool import Toolkit, ToolResponse
from dotenv import load_dotenv

from rag import get_knowledge_base, reset_knowledge_base
from skills_runtime import SkillRegistry

ROOT = Path(__file__).parent
SKILLS_ROOT = ROOT / "skills"

load_dotenv(ROOT / ".env", override=True)

_skill_registry: Optional[SkillRegistry] = None
_toolkit: Optional[Toolkit] = None
_mcp_clients: list[Any] = []
_model: Optional[AnthropicChatModel | OpenAIChatModel] = None

def get_skill_registry() -> SkillRegistry:
    """Load Skill metadata without expanding all Skill bodies."""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry(SKILLS_ROOT)
    return _skill_registry


async def reset_skill_runtime() -> None:
    """Reload skill metadata/toolkit after skill files change."""
    global _skill_registry, _toolkit
    await close_mcp_clients()
    _skill_registry = None
    _toolkit = None
    reset_knowledge_base()


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
    for client in _mcp_clients:
        close = getattr(client, "close", None)
        if close:
            await close(ignore_errors=True)
    _mcp_clients = []


async def get_toolkit() -> Toolkit:
    """Create the AgentScope Toolkit with tools, Skills and MCP clients."""
    global _toolkit
    if _toolkit is not None:
        return _toolkit

    toolkit = Toolkit()
    toolkit.register_tool_function(read_file)
    for skill in get_skill_registry().skills:
        toolkit.register_agent_skill(str(skill.path))
    await register_mcp_servers(toolkit)
    _toolkit = toolkit
    return _toolkit


# ── LLM 客户端 (AgentScope AnthropicChatModel) ─────────────────
_model: Optional[AnthropicChatModel | OpenAIChatModel] = None


def get_model() -> AnthropicChatModel | OpenAIChatModel:
    global _model
    if _model is None:
        provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
        model_name = os.getenv("MODEL_NAME", "deepseek-v4-pro")
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
    provider = os.getenv("MODEL_PROVIDER", "deepseek").lower()
    if provider in {"openai", "deepseek"}:
        return OpenAIChatFormatter()
    return AnthropicChatFormatter()


def build_system_prompt() -> str:
    """Build the agent system prompt; Toolkit appends compact Skill prompts."""
    return f"""你是国网云平台检修方案生成 Agent。

遵循 AgentScope Skill 的渐进式披露原则：先根据 Skill 描述判断任务需要哪些 Skill，再通过 read_file 工具读取对应 SKILL.md。

如果用户消息中包含“编排上下文”，其中的候选 Skill 是后端初筛结果，必须优先读取这些 Skill；RAG 参考资料只作为模板、案例、API 约束和风险控制依据，不得覆盖 Skill 的硬性规则。

最终只输出 JSON，不要输出解释文字。JSON 顶层必须包含 document，document.sections 决定 Word 文档结构。"""


async def get_agent_knowledge():
    """Return AgentScope knowledge object when RAG is configured."""
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is None:
        return None
    return await knowledge_base.get_knowledge()


