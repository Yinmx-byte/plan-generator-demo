"""Impact analysis tool for maintenance operations."""

from __future__ import annotations

import re
from typing import Any, Callable

from agentscope.tool import ToolResponse

from services.impact_analyzer import analyze_impact, render_impact_report
from services.requirements import default_form_state
from .responses import json_tool_response


def _parse_instance_ids(raw: str) -> list[str]:
    """Parse instance IDs from comma/space/newline separated string."""
    if not raw or not raw.strip():
        return []
    ids = re.split(r"[,，\s\n]+", raw.strip())
    return [i for i in ids if i and not i.isspace()]


def build_analyze_impact_tool(session: dict[str, Any]) -> Callable[..., Any]:
    async def analyze_maintenance_impact(
        instance_ids: str = "",
        vpc_id: str = "",
        maintenance_type: str = "",
        region_id: str = "cn-beijing",
    ) -> ToolResponse:
        """Analyze the potential impact of a maintenance operation on associated resources within the same VPC.

        Performs two-layer analysis:
        1. VPC-wide resource discovery (ECS, RDS, SLB, and others)
        2. Security group reference chain tracing

        Use this tool when:
        - The user explicitly asks to "analyze impact" or "what will be affected"
        - The user wants to understand potential blast radius before maintenance
        - After generating a maintenance plan, you may ask the user if they want an impact analysis

        Args:
            instance_ids: Comma/space separated ECS instance IDs. Auto-detected from session state if empty.
            vpc_id: VPC ID for broader analysis when no specific instances are given.
            maintenance_type: Type of maintenance (e.g. 配置变更, 组件扩缩容). Auto-detected from session.
            region_id: Alibaba Cloud region ID. Default "cn-beijing".
        """
        state = session.setdefault("state", default_form_state())

        if not instance_ids:
            instance_ids = state.get("instances", "")
        if not maintenance_type:
            maintenance_type = state.get("maintenance_type", "")

        ids = _parse_instance_ids(instance_ids)

        # Try to extract instance IDs from free-text instances field
        if not ids:
            instances_text = state.get("instances", "")
            # Match i-xxx pattern
            ecs_matches = re.findall(r"i-[a-zA-Z0-9]+", instances_text)
            if ecs_matches:
                ids = ecs_matches

        if not ids and not vpc_id:
            return json_tool_response({
                "status": "missing_target",
                "message": (
                    "未找到目标实例信息。请提供 ECS 实例 ID（如 i-xxx），"
                    "或先通过 update_requirements 补充涉及实例信息。"
                ),
            })

        results = []
        for eid in ids:
            analysis = analyze_impact(eid, region_id=region_id)
            report = render_impact_report(analysis, maintenance_type)
            results.append({
                "instance_id": eid,
                "error": analysis.get("error", ""),
                "vpc_id": analysis.get("vpc_id", ""),
                "report": report,
                "data": analysis,
            })

        # Build a combined response
        if len(results) == 1:
            r = results[0]
            return json_tool_response({
                "status": "error" if r.get("error") else "ok",
                "instance_id": r["instance_id"],
                "vpc_id": r["vpc_id"],
                "report": r["report"],
                "data": r["data"],
            })

        # Multiple instances: combine reports
        combined_report = "\n\n---\n\n".join(
            f"### 实例 {r['instance_id']}\n{r['report']}"
            for r in results
        )
        return json_tool_response({
            "status": "ok",
            "instance_count": len(results),
            "report": combined_report,
            "data": results,
        })

    return analyze_maintenance_impact
