"""Cloud resource query routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from services.aliyun_cloud_sdk import query_ecs_vpc_info

router = APIRouter()


@router.get("/api/cloud/ecs-vpc-info")
@router.get("/api/cloud/ecs-vpc-usage")
async def get_ecs_vpc_info(
    region_id: str = Query(default="cn-beijing", description="Alibaba Cloud region ID"),
    vpc_id: str = Query(default="", description="Optional VPC ID"),
    vpc_name: str = Query(default="", description="Optional VPC name"),
    instance_ids: str = Query(default="", description="Comma-separated ECS instance IDs"),
    include_instances: bool = Query(default=True),
    include_vswitches: bool = Query(default=True),
    include_usage_metrics: bool = Query(default=True),
    metric_names: str = Query(default="CPUUtilization"),
    metric_minutes: int = Query(default=60, ge=1, le=1440),
    metric_period: str = Query(default="300"),
):
    """Query read-only ECS/VPC topology and usage metrics via Alibaba Cloud SDK."""
    try:
        data = await asyncio.to_thread(
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
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "data": data}
