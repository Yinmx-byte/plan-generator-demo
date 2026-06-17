"""Requirement state, missing-field prompts, and extraction helpers."""

import json
import re
from typing import Any

from runtime import get_extraction_model
from services.json_utils import extract_json, get_response_text

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



def default_form_state() -> dict[str, str]:
    return {
        "background": "",
        "maintenance_type": "",
        "network": "",
        "location": "",
        "instances": "",
        "schedule_year": "",
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
    next_labels = (
        "检修背景|检修类型|网络环境|内外网环境|实施地点|涉及实例|涉及的组件实例|检修窗口|"
        "方案提供人|检修执行人|检修复核人|安全责任人|ASCM 授权账号|"
        "ASCM授权账号|堡垒机账号|技术参数|补充要求"
    )

    if text:
        updates["background"] = text

    def labeled_value(*labels: str, multiline: bool = False) -> str:
        label_group = "|".join(re.escape(label) for label in labels)
        if multiline:
            pattern = (
                rf"(?:{label_group})\s*[:：]\s*(.*?)"
                rf"(?=(?:\n|[。；;]\s*)?(?:{next_labels})\s*[:：]|\Z)"
            )
            match = re.search(pattern, text, re.S | re.I)
        else:
            pattern = (
                rf"(?:{label_group})\s*[:：]\s*(.*?)"
                rf"(?=(?:[。；;]\s*)?(?:{next_labels})\s*[:：]|\n|\Z)"
            )
            match = re.search(pattern, text, re.S | re.I)
        return match.group(1).strip(" 。；;\n\t") if match else ""

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

    if updates.get("maintenance_type"):
        return updates

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
        response = await get_extraction_model()(
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
    data["updates"] = {**model_updates, **inferred_updates}
    if not data.get("assistant_note") and data["updates"]:
        data["assistant_note"] = "已收到需求描述，我先整理出可识别的信息。"
    return data


