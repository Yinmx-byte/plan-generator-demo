"""Read-only Alibaba Cloud resource query tools."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from agentscope.tool import ToolResponse

from services.aliyun_cloud_sdk import (
    query_cloud_inventory,
    query_cloud_metrics,
    query_ecs_vpc_info,
    query_resource_group_products,
)

from .responses import json_tool_response


def build_query_ecs_vpc_info_tool() -> Callable[..., ToolResponse]:
    async def query_ecs_vpc_info_tool(
        region_id: str = "cn-beijing",
        vpc_id: str = "",
        vpc_name: str = "",
        instance_ids: str = "",
        include_instances: bool = True,
        include_vswitches: bool = True,
        include_usage_metrics: bool = True,
        metric_names: str = "CPUUtilization",
        metric_minutes: int = 60,
        metric_period: str = "300",
    ) -> ToolResponse:
        """Query read-only ECS/VPC topology and usage metrics from Alibaba Cloud SDK.

        Args:
            region_id: Alibaba Cloud region ID, for example cn-beijing.
            vpc_id: Optional VPC ID used to narrow the query.
            vpc_name: Optional VPC name used to narrow the query.
            instance_ids: Optional comma-separated ECS instance IDs or JSON array string.
            include_instances: Whether to include related ECS instances.
            include_vswitches: Whether to include related VSwitches.
            include_usage_metrics: Whether to query recent ECS usage metrics from CloudMonitor.
            metric_names: Comma-separated CloudMonitor metric names.
            metric_minutes: Recent time window in minutes for metrics.
            metric_period: CloudMonitor metric period in seconds.
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
                include_usage_metrics=include_usage_metrics,
                metric_names=metric_names,
                metric_minutes=metric_minutes,
                metric_period=metric_period,
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


def build_query_cloud_inventory_tool() -> Callable[..., ToolResponse]:
    async def query_cloud_inventory_tool(
        product: str = "ecs",
        region_id: str = "cn-beijing",
        vpc_id: str = "",
        vpc_name: str = "",
        instance_ids: str = "",
        include_instances: bool = True,
        include_vswitches: bool = True,
    ) -> ToolResponse:
        """Query read-only cloud resource inventory by product.

        Args:
            product: Product key or alias, for example ecs, vpc, 云服务器.
            region_id: Alibaba Cloud region ID, for example cn-beijing.
            vpc_id: Optional VPC ID used to narrow ECS/VPC inventory.
            vpc_name: Optional VPC name used to narrow ECS/VPC inventory.
            instance_ids: Optional comma-separated ECS instance IDs or JSON array string.
            include_instances: Whether to include ECS instances.
            include_vswitches: Whether to include VSwitches.
        """
        try:
            result = await asyncio.to_thread(
                query_cloud_inventory,
                product=product,
                region_id=region_id,
                vpc_id=vpc_id,
                vpc_name=vpc_name,
                instance_ids=instance_ids,
                include_instances=include_instances,
                include_vswitches=include_vswitches,
            )
            return json_tool_response({"status": "ok", "data": result})
        except Exception as exc:
            return json_tool_response({"status": "error", "message": str(exc)})

    return query_cloud_inventory_tool


def build_query_cloud_metrics_tool() -> Callable[..., ToolResponse]:
    async def query_cloud_metrics_tool(
        product: str = "ecs",
        metric: str = "cpu_usage",
        region_id: str = "cn-beijing",
        instance_ids: str = "",
        metric_minutes: int = 60,
        metric_period: str = "300",
        extra_dimensions_json: str = "",
    ) -> ToolResponse:
        """Query read-only cloud monitoring metrics through configured metric keys.

        Args:
            product: Product key or alias, currently ecs is implemented.
            metric: Metric key or alias, for example cpu_usage, 内存使用率, 磁盘使用率.
            region_id: Alibaba Cloud region ID, for example cn-beijing.
            instance_ids: Required comma-separated ECS instance IDs or JSON array string.
            metric_minutes: Recent time window in minutes.
            metric_period: CloudMonitor metric period in seconds.
            extra_dimensions_json: Optional JSON object for dimensions such as {"device": "/dev/vda1"}.
        """
        try:
            extra_dimensions = json.loads(extra_dimensions_json) if extra_dimensions_json.strip() else {}
            if not isinstance(extra_dimensions, dict):
                raise ValueError("extra_dimensions_json must be a JSON object")
            result = await asyncio.to_thread(
                query_cloud_metrics,
                product=product,
                metric=metric,
                region_id=region_id,
                instance_ids=instance_ids,
                metric_minutes=metric_minutes,
                metric_period=metric_period,
                extra_dimensions=extra_dimensions,
            )
            return json_tool_response({"status": "ok", "data": result})
        except Exception as exc:
            return json_tool_response({"status": "error", "message": str(exc)})

    return query_cloud_metrics_tool


def build_query_resource_group_products_tool() -> Callable[..., ToolResponse]:
    async def query_resource_group_products_tool(
        resource_group_id: str = "",
        region_id: str = "cn-hangzhou",
        max_results: int = 100,
        max_pages: int = 20,
    ) -> ToolResponse:
        """Count product categories in accessible Resource Center resources.

        Args:
            resource_group_id: Optional Alibaba Cloud resource group ID. Leave empty for all accessible resources.
            region_id: Resource Center OpenAPI region, usually cn-hangzhou.
            max_results: Page size for Resource Center SearchResources.
            max_pages: Maximum pages to read.
        """
        try:
            result = await asyncio.to_thread(
                query_resource_group_products,
                resource_group_id=resource_group_id,
                region_id=region_id,
                max_results=max_results,
                max_pages=max_pages,
            )
            return json_tool_response({"status": "ok", "data": result})
        except Exception as exc:
            return json_tool_response({"status": "error", "message": str(exc)})

    return query_resource_group_products_tool
