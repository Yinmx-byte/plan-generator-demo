"""Historical maintenance record lookup and report generation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Callable

from agentscope.tool import ToolResponse

from runtime import get_extraction_model
from services.json_utils import extract_json, get_response_text
from services.plan_archive import (
    _extract_action,
    _extract_product_type,
    _extract_system_name,
    get_archive_store,
)
from services.requirements import default_form_state
from .responses import json_tool_response

HISTORY_REPORT_PROMPT = """你是检修方案历史记录分析助手。请基于用户当前待执行的检修任务以及匹配到的历史检修记录，生成一份简短的概览总结。

## 当前检修任务
{current_task}

## 匹配到的历史记录（共 {count} 条，按相关度排序）
{records_summary}

## 要求
请输出 JSON，格式如下：
{{
  "summary": "一段 2-4 句话的概览总结。必须包含：查询日期范围、记录总数、产品类型分布（如 ECS X条、OSS Y条）、涉及的系统名称、最近一次检修的日期和操作类型、是否有值得注意的模式（如多次迭代、同一执行人等）。语言简洁自然，信息密集，不需要问候语。"
}}

只输出 JSON，不要 markdown 代码块包裹，不要解释。"""


def _infer_product_type(state: dict) -> str:
    """Infer product type from session state fields.

    Returns empty string when no specific product can be identified.
    "通用" is treated as unknown to avoid filtering out all records.
    """
    instances = state.get("instances", "") or ""
    tech_params = state.get("tech_params", "") or ""
    maintenance_type = state.get("maintenance_type", "") or ""
    background = state.get("background", "") or ""
    combined = f"{instances} {tech_params} {maintenance_type} {background}"
    if not combined.strip():
        return ""
    result = _extract_product_type("", instances, tech_params)
    if not result or result == "通用":
        return ""
    return result


def _infer_action(state: dict) -> str:
    """Infer maintenance action from session state fields.

    Returns empty string when no specific action can be identified.
    "检修" is the generic fallback and treated as unknown.
    """
    maintenance_type = state.get("maintenance_type", "") or ""
    background = state.get("background", "") or ""
    instances = state.get("instances", "") or ""
    combined = f"{maintenance_type} {background} {instances}"
    if not combined.strip():
        return ""
    result = _extract_action(combined, instances, maintenance_type)
    if not result or result == "检修":
        return ""
    return result


def _infer_system_name(state: dict) -> str:
    """Extract system name from session state, using the same logic as the archive store."""
    instances = state.get("instances", "") or ""
    title = state.get("title", "") or ""
    result = _extract_system_name(instances, title)
    return "" if result == "未知系统" else result


def _score_record(record: dict, state: dict) -> int:
    """Score an archive record against the current session state. Higher is better."""
    score = 0

    target_product = _infer_product_type(state)
    if target_product and record.get("product_type", "").lower() == target_product.lower():
        score += 5

    target_action = _infer_action(state)
    record_action = record.get("action", "")
    if target_action and record_action:
        if target_action == record_action:
            score += 4
        elif target_action in record_action or record_action in target_action:
            score += 2

    target_network = (state.get("network") or "").strip()
    record_network = (record.get("network") or "").strip()
    if target_network and record_network and target_network == record_network:
        score += 2

    target_system = _infer_system_name(state)
    record_system = record.get("system_name", "") or ""
    if target_system and record_system:
        ratio = SequenceMatcher(None, target_system.lower(), record_system.lower()).ratio()
        if ratio > 0.8:
            score += 3
        elif ratio > 0.5:
            score += 1

    archive_date = record.get("archive_date", "") or ""
    try:
        record_date = datetime.strptime(archive_date, "%Y-%m-%d")
        days_ago = (datetime.now() - record_date).days
        if days_ago <= 30:
            score += 3
        elif days_ago <= 90:
            score += 1
    except (ValueError, TypeError):
        pass

    for person_field in ("provider", "executor", "reviewer"):
        state_person = (state.get(person_field) or "").strip()
        record_person = (record.get(person_field) or "").strip()
        if state_person and record_person and state_person == record_person:
            score += 1

    return score


def _score_and_rank(records: list[dict], state: dict, max_records: int) -> list[tuple[dict, int]]:
    """Score, rank, and return top-N records with their scores."""
    scored = [(record, _score_record(record, state)) for record in records]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:max_records]


def _summarize_record(record: dict, score: int) -> dict:
    """Extract key fields from an archive record for the LLM prompt."""
    return {
        "id": record.get("id"),
        "series_id": record.get("series_id", ""),
        "version": record.get("version", 1),
        "title": record.get("title", ""),
        "product_type": record.get("product_type", ""),
        "action": record.get("action", ""),
        "system_name": record.get("system_name", ""),
        "schedule_start": record.get("schedule_start", ""),
        "provider": record.get("provider", ""),
        "executor": record.get("executor", ""),
        "reviewer": record.get("reviewer", ""),
        "business_impact": record.get("business_impact", ""),
        "rollback_method": record.get("rollback_method", ""),
        "key_params": record.get("key_params", ""),
        "change_summary": record.get("change_summary", ""),
        "archive_date": record.get("archive_date", ""),
        "match_score": score,
    }


def _compute_simple_summary(scored: list[tuple[dict, int]], start_date: str, end_date: str) -> str:
    """Generate a statistics-based summary without LLM (fallback)."""
    if not scored:
        return ""
    product_counts: dict[str, int] = {}
    systems: set[str] = set()
    latest_date = ""
    latest_title = ""
    for record, _ in scored:
        pt = record.get("product_type", "未知")
        product_counts[pt] = product_counts.get(pt, 0) + 1
        sn = record.get("system_name", "")
        if sn:
            systems.add(sn)
        archive_date = record.get("archive_date", "")
        if not latest_date or archive_date > latest_date:
            latest_date = archive_date
            latest_title = record.get("title", "")
    product_parts = [f"{pt} {cnt}条" for pt, cnt in product_counts.items()]
    system_str = "、".join(systems) if systems else "多个系统"
    lines = [
        f"在 {start_date} 至 {end_date} 期间，共找到 **{len(scored)}** 条检修记录，"
        f"涉及 {len(product_counts)} 种产品（{'、'.join(product_parts)}），"
        f"覆盖系统：{system_str}。"
    ]
    if latest_date and latest_title:
        lines.append(f"最近一次为 {latest_date} 归档的「{latest_title[:40]}」。")
    return "".join(lines)


def _render_record_list(scored: list[tuple[dict, int]]) -> str:
    """Render each matched record as a formatted list item."""
    if not scored:
        return ""
    lines = ["### 记录列表\n"]
    for i, (record, _score) in enumerate(scored, 1):
        title = record.get("title", "无标题")
        version = record.get("version", 1)
        lines.append(f"**{i}. {title}** (v{version})")
        product = record.get("product_type", "")
        action = record.get("action", "")
        schedule = (record.get("schedule_start") or "-")[:10]
        lines.append(f"- 产品: {product} | 动作: {action} | 检修日期: {schedule}")
        personnel = "/".join(filter(None, [
            record.get("provider", ""),
            record.get("executor", ""),
            record.get("reviewer", ""),
        ]))
        if personnel:
            lines.append(f"- 人员: {personnel}")
        impact = record.get("business_impact", "")
        if impact:
            lines.append(f"- 业务影响: {impact[:80]}")
        rollback = record.get("rollback_method", "")
        if rollback:
            lines.append(f"- 回滚方式: {rollback[:80]}")
        params = record.get("key_params", "")
        if params:
            lines.append(f"- 关键参数: {params[:100]}")
        lines.append("")
    return "\n".join(lines)


async def _generate_report(
    scored: list[tuple[dict, int]], state: dict, start_date: str = "", end_date: str = ""
) -> str:
    """Generate a concise markdown report from matched historical records using the extraction model."""
    if not scored:
        return "未找到相关的历史检修记录。"

    records_json = json.dumps(
        [_summarize_record(r, s) for r, s in scored],
        ensure_ascii=False,
        indent=2,
    )

    current_task_parts = []
    product = _infer_product_type(state)
    action = _infer_action(state)
    system = _infer_system_name(state)
    if product:
        current_task_parts.append(f"产品类型: {product}")
    if action:
        current_task_parts.append(f"检修动作: {action}")
    if system:
        current_task_parts.append(f"目标系统: {system}")
    schedule = state.get("schedule_start", "") or ""
    if schedule:
        current_task_parts.append(f"计划时间: {schedule}")
    current_task = "\n".join(current_task_parts) if current_task_parts else "（待补充详细信息）"

    prompt = HISTORY_REPORT_PROMPT.format(
        current_task=current_task,
        count=len(scored),
        records_summary=records_json,
    )

    try:
        model = get_extraction_model()
        response = await model(prompt)
        text = get_response_text(response)
        result = extract_json(text)
    except Exception:
        result = {}

    summary = result.get("summary", "")
    if not summary:
        summary = _compute_simple_summary(scored, start_date, end_date)

    lines = [f"## 历史检修记录查询结果\n"]
    lines.append(f"> {summary}")
    lines.append("")
    lines.append(_render_record_list(scored))
    return "\n".join(lines)


def _fallback_report(scored: list[tuple[dict, int]], start_date: str = "", end_date: str = "") -> str:
    """Generate a simple template-based report when LLM is unavailable."""
    if not scored:
        return "未找到相关的历史检修记录。"
    summary = _compute_simple_summary(scored, start_date, end_date)
    lines = [f"## 历史检修记录查询结果\n"]
    lines.append(f"> {summary}")
    lines.append("")
    lines.append(_render_record_list(scored))
    return "\n".join(lines)


def build_lookup_history_tool(session: dict[str, Any]) -> Callable[..., Any]:
    async def lookup_maintenance_history(
        product_type: str = "",
        action: str = "",
        system_name: str = "",
        start_date: str = "",
        end_date: str = "",
        max_records: int = 5,
    ) -> ToolResponse:
        """Look up historical maintenance records for the same or similar systems and generate a brief analysis report.

        Use this tool when:
        - The session has collected enough context (product type, action, system name) and you want to
          provide the user with historical reference before generating a new maintenance plan.
        - The user explicitly asks about past maintenance records, similar operations, or historical patterns.

        The tool queries the maintenance plan archive, scores records by relevance, and returns a
        structured report with patterns, risk reminders, and reusable practices.

        Args:
            product_type: Cloud product filter (e.g. ECS, RDS, PolarDB). Auto-detected from session if empty.
            action: Maintenance action filter (e.g. 磁盘扩容, 创建实例). Auto-detected from session if empty.
            system_name: System name for fuzzy matching. Auto-detected from session if empty.
            start_date: Search start date in YYYY-MM-DD format. When the user specifies a time range like
                "2025年", "去年", "最近3个月", "2025年3月到6月", convert it to a concrete date range and pass
                both start_date and end_date. Leave empty to default to the last 12 months.
            end_date: Search end date in YYYY-MM-DD format. Defaults to today if start_date is provided
                but end_date is not. Leave empty to default to the last 12 months.
            max_records: Maximum records to include in the report (1-20). Default 5.
        """
        state = session.setdefault("state", default_form_state())

        if not product_type:
            product_type = _infer_product_type(state)
        if not action:
            action = _infer_action(state)
        if not system_name:
            system_name = _infer_system_name(state)

        max_records = max(1, min(20, max_records))

        # Resolve date range: explicit dates take priority, otherwise default to last 12 months
        if start_date or end_date:
            resolved_start = start_date if start_date else "2020-01-01"
            resolved_end = end_date if end_date else datetime.now().strftime("%Y-%m-%d")
        else:
            resolved_end = datetime.now().strftime("%Y-%m-%d")
            resolved_start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        filters: dict[str, str] = {
            "start_date": resolved_start,
            "end_date": resolved_end,
        }
        if product_type:
            filters["product_type"] = product_type
        if action:
            filters["action"] = action
        if system_name:
            filters["system_name"] = system_name

        try:
            store = get_archive_store()
        except Exception:
            return json_tool_response({
                "status": "archive_unavailable",
                "message": "归档存储未配置或不可用，无法查询历史记录。",
            })

        try:
            records = store.query_summary(filters, latest_only=True)
        except Exception:
            return json_tool_response({
                "status": "archive_unavailable",
                "message": "查询归档库失败，请检查数据库连接。",
            })

        if not records:
            return json_tool_response({
                "status": "no_records",
                "total_found": 0,
                "matched_count": 0,
                "filters_applied": {
                    "product_type": product_type,
                    "action": action,
                    "system_name": system_name,
                    "start_date": resolved_start,
                    "end_date": resolved_end,
                },
                "report": (
                    f"未找到相关的历史检修记录。"
                    f"在 {resolved_start} 至 {resolved_end} 期间没有匹配的归档记录。"
                ),
                "records": [],
            })

        scored = _score_and_rank(records, state, max_records)
        report = await _generate_report(scored, state, resolved_start, resolved_end)

        return json_tool_response({
            "status": "ok",
            "total_found": len(records),
            "matched_count": len(scored),
            "filters_applied": {
                "product_type": product_type,
                "action": action,
                "system_name": system_name,
                "start_date": resolved_start,
                "end_date": resolved_end,
            },
            "report": report,
            "records": [_summarize_record(r, s) for r, s in scored],
        })

    return lookup_maintenance_history
