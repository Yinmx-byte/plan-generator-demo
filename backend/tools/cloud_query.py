"""Read-only Alibaba Cloud resource query tools."""

from __future__ import annotations

import asyncio
from typing import Callable

from agentscope.tool import ToolResponse

from services.aliyun_cloud_sdk import query_ecs_vpc_info

from .responses import json_tool_response


def build_query_ecs_vpc_info_tool() -> Callable[..., ToolResponse]:
    async def query_ecs_vpc_info_tool(
        region_id: str = "cn-beijing",
        vpc_id: str = "",
        vpc_name: str = "",
        instance_ids: str = "",
        include_instances: bool = True,
        include_vswitches: bool = True,
    ) -> ToolResponse:
        """Query read-only ECS/VPC topology information from Alibaba Cloud SDK.

        Args:
            region_id: Alibaba Cloud region ID, for example cn-beijing.
            vpc_id: Optional VPC ID used to narrow the query.
            vpc_name: Optional VPC name used to narrow the query.
            instance_ids: Optional comma-separated ECS instance IDs or JSON array string.
            include_instances: Whether to include related ECS instances.
            include_vswitches: Whether to include related VSwitches.
        """
        try:
            result = await asyncio.to_thread(
                query_ecs_vpc_info,
                region_id=region_id,
                vpc_id=vpc_id,
                vpc_name=vpc_name,
                instance_ids=instance_ids,
                include_instances=include_instances,
                include_vswitches=include_vswitches,
            )
            return json_tool_response({"status": "ok", "data": result})
        except Exception as exc:
            return json_tool_response(
                {
                    "status": "error",
                    "message": str(exc),
                    "hint": "确认已配置阿里云 ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET，且账号具备 ECS/VPC 只读权限。",
                }
            )

    return query_ecs_vpc_info_tool
