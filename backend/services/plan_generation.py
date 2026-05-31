"""Maintenance plan generation service."""

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from agents.plan_agent import PlanAgentRuntime, run_plan_agent
from rag import get_knowledge_base
from runtime import (
    SKILLS_ROOT,
    build_system_prompt,
    get_agent_knowledge,
    get_formatter,
    get_model,
    get_skill_registry,
    get_toolkit,
)
from scripts.generate_plan import build_document
from services.json_utils import extract_json, get_response_text
from services.requirements import FORM_FIELDS

_generated_files: dict[str, Path] = {}


def get_plan_agent_runtime() -> PlanAgentRuntime:
    """Wire runtime dependencies for the maintenance plan generation agent."""
    return PlanAgentRuntime(
        build_system_prompt=build_system_prompt,
        get_model=get_model,
        get_formatter=get_formatter,
        get_toolkit=get_toolkit,
        get_agent_knowledge=get_agent_knowledge,
        get_response_text=get_response_text,
    )


def get_generated_file(file_id: str | None) -> Optional[Path]:
    path = _generated_files.get(file_id or "")
    return path if path and path.exists() else None


def get_generated_path(session: dict[str, Any]) -> Optional[Path]:
    generated = session.get("generated") or {}
    return get_generated_file(generated.get("file_id"))


async def build_generation_orchestration_context(state: dict[str, str]) -> dict[str, Any]:
    state_text = "\n".join(
        f"{field}: {state.get(field, '')}"
        for field in FORM_FIELDS
        if state.get(field, "").strip()
    )
    selected_skills = get_skill_registry().select_skills(
        state.get("maintenance_type", ""),
        state_text,
    )

    skill_text = "\n".join(
        f"{skill.name}: {skill.description}" for skill in selected_skills
    )
    rag_chunks: list[str] = []
    knowledge_base = get_knowledge_base(SKILLS_ROOT)
    if knowledge_base is not None:
        rag_query = f"""检修方案参考资料检索
检修类型：{state.get("maintenance_type", "")}
网络环境：{state.get("network", "")}
实施地点：{state.get("location", "")}
涉及实例：{state.get("instances", "")}
技术参数：{state.get("tech_params", "")}
补充要求：{state.get("ops_detail", "")}
候选 Skill：
{skill_text}
请检索相似内部模板、阿里云通用检修方案、风险控制、前置检查、实施步骤、回退和验证要求。"""
        try:
            rag_chunks = await knowledge_base.retrieve(
                rag_query,
                top_k=int(os.getenv("PLAN_RAG_TOP_K", os.getenv("RAG_TOP_K", "5"))),
            )
        except Exception:
            rag_chunks = []

    if selected_skills:
        selected_skill_context = "\n".join(
            [
                "系统已根据检修类型和需求内容初筛出候选 Skill。你必须优先读取这些 Skill 的 SKILL.md；如判断不充分，再结合已注册 Skill 追加读取。",
                *[
                    f"- name: {skill.name}\n"
                    f"  description: {skill.description}\n"
                    f"  skill_dir: {skill.path}\n"
                    f"  skill_file: {skill.path / 'SKILL.md'}"
                    for skill in selected_skills
                ],
            ]
        )
    else:
        selected_skill_context = "未命中明确候选 Skill，请根据已注册 Skill 自行判断。"

    if rag_chunks:
        blocks = []
        max_chars = int(os.getenv("PLAN_RAG_CONTEXT_MAX_CHARS", "6000"))
        used = 0
        for idx, chunk in enumerate(rag_chunks, start=1):
            clean = chunk.strip()
            if not clean:
                continue
            room = max_chars - used
            if room <= 0:
                break
            clean = clean[:room]
            used += len(clean)
            blocks.append(f"[RAG-{idx}]\n{clean}")
        rag_context = "\n\n".join(blocks) if blocks else "当前未检索到 RAG 参考资料。"
    else:
        rag_context = "当前未检索到 RAG 参考资料。若后续配置 embedding API 并添加 backend/knowledge 文档，这里会注入内部模板、历史方案和阿里云通用方案片段。"

    return {
        "selected_skill_names": [skill.name for skill in selected_skills],
        "rag_enabled": knowledge_base is not None,
        "rag_chunks_count": len(rag_chunks),
        "prompt_context": (
            "## 编排上下文：Skill 初筛结果\n"
            f"{selected_skill_context}\n\n"
            "## 编排上下文：RAG 参考资料\n"
            f"{rag_context}\n\n"
            "## 使用要求\n"
            "- Skill 是主规则来源：文档结构、必填章节、风险点、实施步骤和脚本模板优先遵循 Skill。\n"
            "- RAG 是参考依据：用于补充内部模板措辞、历史方案经验、阿里云通用方案/API 约束，不得覆盖 Skill 的硬性规则。\n"
            "- 输出 JSON 中建议包含 evidence 字段，记录 selected_skills 和 rag_chunks_count，便于后续审计。\n"
        ),
    }


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


def write_model_output_debug(text: str, prefix: str = "plan_model_output") -> Path:
    output_dir = Path(tempfile.gettempdir()) / "plan-generator"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(text or "", encoding="utf-8")
    return path


def build_fallback_plan_data(
    state: dict[str, str],
    orchestration: dict[str, Any],
    raw_text: str = "",
) -> dict[str, Any]:
    """Build a conservative document spec when model JSON cannot be parsed."""
    maintenance_type = state.get("maintenance_type") or "检修方案"
    title = f"{maintenance_type}检修方案"
    checked_type = maintenance_type.strip()
    type_names = ["配置变更", "组件升级", "组件扩缩容", "数据库变更", "日常维护（原硬件设备）", "其他"]
    checkbox_items = [
        {
            "label": name,
            "checked": name == checked_type or (name != "其他" and name in checked_type),
            "extra": checked_type if name == "其他" and checked_type not in type_names else "",
        }
        for name in type_names
    ]
    if not any(item["checked"] for item in checkbox_items):
        checkbox_items[-1]["checked"] = True
        checkbox_items[-1]["extra"] = checked_type

    schedule_text = " ".join(
        value
        for value in [
            state.get("schedule_year", ""),
            state.get("schedule_start", ""),
            "至" if state.get("schedule_start") or state.get("schedule_end") else "",
            state.get("schedule_end", ""),
        ]
        if value
    )
    ops_hint = state.get("ops_detail") or "按对应检修类型 Skill 进行前置检查、变更实施、结果验证和回滚准备。"
    tech_hint = state.get("tech_params") or "无额外技术参数。"
    text_blob = "\n".join(
        [
            state.get("background", ""),
            state.get("maintenance_type", ""),
            state.get("instances", ""),
            state.get("tech_params", ""),
            state.get("ops_detail", ""),
        ]
    ).lower()
    if "ecs" in text_blob and any(word in text_blob for word in ["创建", "新建", "申请"]):
        item_name = state.get("background") or state.get("instances") or "创建ecs实例"
        organization = "待实施前确认"
        resource_set = "待实施前确认"
        return {
            "title": f"{item_name}检修方案",
            "department": "云运营中心平台运维处",
            "date": datetime.now().strftime("%Y年%m月%d日"),
            "evidence": {
                "selected_skills": orchestration.get("selected_skill_names", []),
                "rag_enabled": orchestration.get("rag_enabled", False),
                "rag_chunks_count": orchestration.get("rag_chunks_count", 0),
                "fallback_used": True,
                "raw_output_saved": bool(raw_text),
            },
            "document": {
                "title": f"{item_name}检修方案",
                "cover": {"logo_width_cm": 3.1, "top_spacers": 7, "middle_spacers": 8},
                "header": [
                    {"text": "云运营中心平台运维处", "font_size": 14, "align": "center"},
                    {"text": datetime.now().strftime("%Y年%m月%d日"), "font_size": 12, "align": "center"},
                ],
                "sections": [
                    {
                        "heading": "背景",
                        "blocks": [
                            {"type": "paragraph", "text": item_name},
                            {"type": "paragraph", "text": "该事项项目组提报问题工单，需检修进行处理，实现问题工单闭环。"},
                        ],
                    },
                    {"heading": "检修类型", "blocks": [{"type": "checkbox_group", "items": checkbox_items, "per_line": 3}]},
                    {
                        "heading": "现场环境",
                        "blocks": [
                            {"type": "paragraph", "text": f"（1）内网环境/外网环境：{state.get('network', '')}"},
                            {"type": "paragraph", "text": f"（2）实施地点：{state.get('location', '')}"},
                            {"type": "paragraph", "text": "（3）专有云版本：v3.16"},
                            {"type": "paragraph", "text": "（4）涉及的组件实例信息："},
                            {"type": "paragraph", "text": f"1、{item_name}"},
                            {"type": "paragraph", "text": f"组织：{organization}"},
                            {"type": "paragraph", "text": f"资源集：{resource_set}"},
                            {
                                "type": "table",
                                "columns": [
                                    {"key": "cloud_env", "label": "云环境"},
                                    {"key": "instance_name", "label": "实例名称"},
                                    {"key": "disk", "label": "磁盘"},
                                    {"key": "image", "label": "自定义镜像"},
                                    {"key": "password", "label": "密码"},
                                    {"key": "vpc", "label": "VPC ID或名称"},
                                    {"key": "vswitch", "label": "Vswitch ID或名称"},
                                    {"key": "security_group", "label": "安全组"},
                                    {"key": "spec", "label": "实例规格"},
                                    {"key": "count", "label": "数量"},
                                ],
                                "rows": [
                                    {
                                        "cloud_env": state.get("network", ""),
                                        "instance_name": state.get("instances", "") or "待实施前确认",
                                        "disk": "待实施前确认",
                                        "image": "待实施前确认",
                                        "password": "按 ASCM 平台规范生成，方案不明文展示",
                                        "vpc": "待实施前确认",
                                        "vswitch": "待实施前确认",
                                        "security_group": "待实施前确认",
                                        "spec": "待实施前确认",
                                        "count": "待实施前确认",
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "heading": "实施计划",
                        "blocks": [
                            {"type": "heading", "text": "4.1 检修窗口", "level": 2},
                            {"type": "table", "columns": [{"key": "year", "label": "年份"}, {"key": "start_time", "label": "开始时间"}, {"key": "end_time", "label": "结束时间"}], "rows": [{"year": state.get("schedule_year", ""), "start_time": state.get("schedule_start", ""), "end_time": state.get("schedule_end", "")}]},
                            {"type": "heading", "text": "4.2 实施人员", "level": 2},
                            {"type": "table", "columns": [{"key": "provider", "label": "方案提供人"}, {"key": "executor", "label": "检修执行人"}, {"key": "reviewer", "label": "检修复核人"}, {"key": "business_participant", "label": "业务系统参与人"}, {"key": "security_officer", "label": "安全责任人"}], "rows": [{"provider": state.get("provider", ""), "executor": state.get("executor", ""), "reviewer": state.get("reviewer", ""), "business_participant": "不涉及", "security_officer": state.get("security_officer", "")}]},
                        ],
                    },
                    {
                        "heading": "风险评估",
                        "blocks": [
                            {"type": "heading", "text": "5.1影响范围", "level": 2},
                            {"type": "paragraph", "text": f"{item_name}对业务无影响；"},
                            {"type": "heading", "text": "5.2危险点分析", "level": 2},
                            {"type": "paragraphs", "items": [
                                "（1）授权不当危险点：授权过大，导致操作影响预定方案以外的生产环境实例。",
                                "（2）备份不当危险点：本次创建新实例不涉及业务数据备份，但需保留创建参数用于回滚删除核对。",
                                "（3）验证不当危险点：ECS操作对象，以及组织、资源集、VPC、VSwitch、安全组、镜像、规格、IP地址余量未核实清楚，导致创建失败或业务不可达。",
                                "（4）双人复核不当危险点：双人复核不仔细，导致操作错误执行而出现业务影响。",
                            ]},
                            {"type": "heading", "text": "5.3安全措施", "level": 2},
                            {"type": "heading", "text": "5.3.1授权", "level": 3},
                            {"type": "paragraphs", "items": [f"ASCM：国网总部直属单位权限     授权账号：{state.get('ascm_account', '')}", f"堡垒机账号：{state.get('bastion_account', '')}"]},
                            {"type": "heading", "text": "5.3.2备份", "level": 3},
                            {"type": "paragraph", "text": f"(1){item_name}不涉及备份"},
                            {"type": "heading", "text": "5.3.3验证", "level": 3},
                            {"type": "paragraph", "text": f"(1){item_name}检查资源集IP充足，确认VPC、VSwitch、安全组、镜像、规格、磁盘、数量与需求一致。"},
                            {"type": "heading", "text": "5.3.4 双人复核", "level": 3},
                            {"type": "paragraphs", "items": ["(1)确认在正确的组织和资源集下做操作，检查实例操作对象是否正确；", "(2)严格按照文档复核关键步骤及关键点。"]},
                        ],
                    },
                    {
                        "heading": "实施步骤",
                        "blocks": [
                            {"type": "heading", "text": "6.1备份", "level": 2},
                            {"type": "numbered_list", "items": [f"{item_name}不涉及备份"]},
                            {"type": "heading", "text": "6.2 检修前验证", "level": 2},
                            {"type": "numbered_list", "items": [f"{item_name}检查资源集IP充足", f"确认组织“{organization}”、资源集“{resource_set}”、云环境“{state.get('network', '')}”与工单需求一致。", "确认VPC、VSwitch、安全组、镜像、实例规格、磁盘、数量已由双人复核。"]},
                            {"type": "heading", "text": "6.3 检修操作", "level": 2},
                            {"type": "heading", "text": f"6.3.1 {item_name}", "level": 3},
                            {"type": "paragraphs", "items": [f"使用{state.get('ascm_account', '')}账号，登录{state.get('network', '')}ASCM平台，产品选择“云服务器ECS”。", f"选择组织“{organization}”-资源集“{resource_set}”，进入云服务器ECS实例列表。", "点击“创建”或“创建ECS实例”。", "按参数表填写云环境、实例名称、磁盘、自定义镜像、VPC、VSwitch、安全组、实例规格、数量等内容。", "确认填写内容无误后，由检修复核人进行关键参数复核。", "复核通过后点击提交，等待创建任务完成。", "创建完成后进入实例列表，确认新建ECS实例状态为运行中或正常，记录实例ID、IP地址和所属资源集。"]},
                            {"type": "heading", "text": "6.4 检修后验证", "level": 2},
                            {"type": "numbered_list", "items": [f"验证{item_name}实例状态正常；", "核对实例名称、规格、磁盘、镜像、VPC、VSwitch、安全组、IP、资源集与参数表一致；", "联系项目组验证业务正常；", "保留ASCM创建结果截图、实例列表截图和项目组验证记录。"]},
                        ],
                    },
                    {
                        "heading": "回滚步骤",
                        "blocks": [
                            {"type": "heading", "text": "7.1 回滚操作", "level": 2},
                            {"type": "numbered_list", "items": [f"{item_name}回退", "删除新建ecs实例", "确认实例列表中已不存在本次新建实例，或实例处于已释放状态。"]},
                            {"type": "heading", "text": "7.2 回滚后验证", "level": 2},
                            {"type": "numbered_list", "items": [f"{item_name}回退验证", "验证回退正常；联系项目组验证业务正常。"]},
                        ],
                    },
                ],
            },
        }

    return {
        "title": title,
        "department": "云运营中心平台运维处",
        "date": datetime.now().strftime("%Y年%m月%d日"),
        "evidence": {
            "selected_skills": orchestration.get("selected_skill_names", []),
            "rag_enabled": orchestration.get("rag_enabled", False),
            "rag_chunks_count": orchestration.get("rag_chunks_count", 0),
            "fallback_used": True,
            "raw_output_saved": bool(raw_text),
        },
        "document": {
            "title": title,
            "header": [
                {"text": "云运营中心平台运维处", "align": "center"},
                {"text": datetime.now().strftime("%Y年%m月%d日"), "align": "center"},
            ],
            "sections": [
                {
                    "heading": "一、背景",
                    "blocks": [
                        {
                            "type": "numbered_list",
                            "items": [state.get("background") or "根据业务运维需求，需要制定并执行本次检修方案。"],
                            "first_line_indent": 0.74,
                        },
                        {
                            "type": "paragraph",
                            "text": "以上事项由项目组提出检修需求，需通过规范化实施完成问题闭环。",
                            "first_line_indent": 0.74,
                        },
                    ],
                },
                {
                    "heading": "二、检修类型",
                    "blocks": [{"type": "checkbox_group", "items": checkbox_items, "per_line": 2}],
                },
                {
                    "heading": "三、现场环境",
                    "blocks": [
                        {
                            "type": "key_values",
                            "items": [
                                {"label": "网络环境", "value": state.get("network", "")},
                                {"label": "实施地点", "value": state.get("location", "")},
                                {"label": "检修窗口", "value": schedule_text},
                            ],
                        },
                        {"type": "paragraph", "text": "涉及实例信息：", "first_line_indent": 0.74},
                        {"type": "paragraph", "text": state.get("instances") or "待实施前由执行人员再次确认。", "first_line_indent": 0.74},
                    ],
                },
                {
                    "heading": "四、实施计划",
                    "blocks": [
                        {
                            "type": "table",
                            "columns": [
                                {"key": "role", "label": "角色"},
                                {"key": "name", "label": "人员/账号"},
                            ],
                            "rows": [
                                {"role": "方案提供人", "name": state.get("provider", "")},
                                {"role": "检修执行人", "name": state.get("executor", "")},
                                {"role": "检修复核人", "name": state.get("reviewer", "")},
                                {"role": "安全责任人", "name": state.get("security_officer", "")},
                                {"role": "ASCM授权账号", "name": state.get("ascm_account", "")},
                                {"role": "堡垒机账号", "name": state.get("bastion_account", "")},
                            ],
                        }
                    ],
                },
                {
                    "heading": "五、风险评估",
                    "blocks": [
                        {"type": "heading", "text": "5.1 危险点分析", "level": 2},
                        {"type": "plain_list", "prefix": "（1）", "items": ["检修操作可能影响相关云资源或业务访问，需要在窗口期内执行并做好监控。"]},
                        {"type": "heading", "text": "5.2 预控措施", "level": 2},
                        {"type": "plain_list", "prefix": "（1）", "items": ["实施前完成资源状态、权限、备份/快照、监控告警和回滚条件确认。"]},
                        {"type": "heading", "text": "5.3 应急处置", "level": 2},
                        {"type": "plain_list", "prefix": "（1）", "items": ["若出现异常，立即停止后续操作，保留现场信息，按回滚步骤恢复并通知相关责任人。"]},
                    ],
                },
                {
                    "heading": "六、实施步骤",
                    "blocks": [
                        {
                            "type": "numbered_list",
                            "items": [
                                "实施前确认检修对象、窗口期、授权账号和审批工单均已满足要求。",
                                f"依据检修类型“{maintenance_type}”读取对应 Skill 的实施要求，并结合 RAG 参考资料校验操作边界。",
                                f"按检修目标执行操作：{ops_hint}",
                                f"核对关键技术参数：{tech_hint}",
                                "实施完成后检查资源状态、业务连通性、监控告警和日志，确认无异常后关闭检修。",
                            ],
                            "first_line_indent": 0.74,
                        }
                    ],
                },
                {
                    "heading": "七、回滚步骤",
                    "blocks": [
                        {
                            "type": "numbered_list",
                            "items": [
                                "触发回滚条件时立即停止后续变更操作，通知复核人和安全责任人。",
                                "根据实施前确认的备份、快照、原配置或资源状态执行恢复。",
                                "回滚后重新验证业务访问、资源状态、监控告警和日志，形成处置记录。",
                            ],
                            "first_line_indent": 0.74,
                        }
                    ],
                },
            ],
        },
    }


def build_user_prompt(
    background: str, maintenance_type: str, network: str, location: str,
    instances: str, schedule_year: str, schedule_start: str, schedule_end: str,
    provider: str, executor: str, reviewer: str, security_officer: str,
    ascm_account: str, bastion_account: str, ops_detail: str, tech_params: str,
    orchestration_context: str = "",
    edit_instruction: str = "",
    previous_document_text: str = "",
) -> str:
    """Convert form fields into the user task for the Agent."""
    edit_context = ""
    if edit_instruction:
        previous_text = previous_document_text[:8000] if previous_document_text else "No previous document text was available."
        edit_context = f"""
## Document revision task
This is a revision of the previously generated maintenance plan, not a brand-new plan.
Keep the existing structure and unchanged content as much as possible. Only update the
personnel, risks, steps, rollback content, scripts, or other parts explicitly requested.

## Revision request
{edit_instruction}

## Previous document text for reference
{previous_text}
"""
    return f"""请根据以下检修需求生成标准化检修方案 JSON。

你必须先根据系统中注册的 Agent Skills 判断需要哪些 Skill，然后使用 read_file 工具读取对应 SKILL.md。若涉及多个检修类型，应组合多个 Skill。

{orchestration_context}
{edit_context}

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


def docx_to_markdown(raw: bytes) -> str:
    from docx import Document

    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        document = Document(str(tmp_path))
        lines: list[str] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if text:
                lines.append(text)
                lines.append("")
        for table_idx, table in enumerate(document.tables, start=1):
            lines.append(f"表格 {table_idx}")
            rows = []
            for row in table.rows:
                rows.append([cell.text.strip().replace("\n", " ") for cell in row.cells])
            if rows:
                width = max(len(row) for row in rows)
                normalized = [row + [""] * (width - len(row)) for row in rows]
                lines.append("| " + " | ".join(normalized[0]) + " |")
                lines.append("| " + " | ".join(["---"] * width) + " |")
                for row in normalized[1:]:
                    lines.append("| " + " | ".join(row) + " |")
                lines.append("")
        return "\n".join(lines).strip() + "\n"
    finally:
        tmp_path.unlink(missing_ok=True)


async def generate_docx_from_state(
    state: dict[str, str],
    orchestration: Optional[dict[str, Any]] = None,
    trace_callback=None,
    edit_instruction: str = "",
    previous_document_text: str = "",
) -> tuple[str, Path, str]:
    if orchestration is None:
        orchestration = await build_generation_orchestration_context(state)
    user_prompt = build_user_prompt(
        **state,
        orchestration_context=orchestration["prompt_context"],
        edit_instruction=edit_instruction,
        previous_document_text=previous_document_text,
    )
    response = await run_plan_agent(user_prompt, get_plan_agent_runtime(), trace_callback=trace_callback)
    text = get_response_text(response)
    try:
        data = await repair_and_extract_json(text)
    except HTTPException:
        write_model_output_debug(text)
        data = build_fallback_plan_data(state, orchestration, text)

    data.setdefault("department", "云运营中心平台运维处")
    data.setdefault("date", datetime.now().strftime("%Y年%m月%d日"))
    data.setdefault("evidence", {})
    if isinstance(data["evidence"], dict):
        data["evidence"].setdefault(
            "selected_skills",
            orchestration["selected_skill_names"],
        )
        data["evidence"].setdefault("rag_enabled", orchestration["rag_enabled"])
        data["evidence"].setdefault(
            "rag_chunks_count",
            orchestration["rag_chunks_count"],
        )

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


