"""Cloud resource query routes."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query

from services.aliyun_cloud_sdk import (
    query_cloud_inventory,
    query_cloud_metrics,
    query_ecs_vpc_info,
    query_resource_group_products,
)
from services.cloud_query_test import run_cloud_query_test_suite

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


@router.get("/api/cloud/inventory")
async def get_cloud_inventory(
    product: str = Query(default="ecs", description="Product key or alias"),
    region_id: str = Query(default="cn-beijing", description="Alibaba Cloud region ID"),
    vpc_id: str = Query(default="", description="Optional VPC ID"),
    vpc_name: str = Query(default="", description="Optional VPC name"),
    instance_ids: str = Query(default="", description="Comma-separated ECS instance IDs"),
    include_instances: bool = Query(default=True),
    include_vswitches: bool = Query(default=True),
):
    """Query read-only inventory through the config-driven cloud query layer."""
    try:
        data = await asyncio.to_thread(
            query_cloud_inventory,
            product=product,
            region_id=region_id,
            vpc_id=vpc_id,
            vpc_name=vpc_name,
            instance_ids=instance_ids,
            include_instances=include_instances,
            include_vswitches=include_vswitches,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "data": data}


@router.get("/api/cloud/metrics")
async def get_cloud_metrics(
    product: str = Query(default="ecs", description="Product key or alias"),
    metric: str = Query(default="all", description="Metric key or alias"),
    region_id: str = Query(default="cn-beijing", description="Alibaba Cloud region ID"),
    instance_ids: str = Query(default="", description="Comma-separated ECS instance IDs"),
    metric_minutes: int = Query(default=60, ge=1, le=1440),
    metric_period: str = Query(default="300"),
    extra_dimensions_json: str = Query(default="", description="Optional JSON object dimensions"),
):
    """Query monitoring metrics through configured metric keys."""
    try:
        extra_dimensions = json.loads(extra_dimensions_json) if extra_dimensions_json.strip() else {}
        if not isinstance(extra_dimensions, dict):
            raise ValueError("extra_dimensions_json must be a JSON object")
        data = await asyncio.to_thread(
            query_cloud_metrics,
            product=product,
            metric=metric,
            region_id=region_id,
            instance_ids=instance_ids,
            metric_minutes=metric_minutes,
            metric_period=metric_period,
            extra_dimensions=extra_dimensions,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "data": data}


@router.get("/api/cloud/resource-products")
async def get_resource_group_products(
    resource_group_id: str = Query(default="", description="Optional Alibaba Cloud resource group ID"),
    region_id: str = Query(default="cn-hangzhou", description="Resource Center OpenAPI region"),
    max_results: int = Query(default=100, ge=1, le=100),
    max_pages: int = Query(default=20, ge=1, le=100),
):
    """Count product categories in accessible Resource Center resources."""
    try:
        data = await asyncio.to_thread(
            query_resource_group_products,
            resource_group_id=resource_group_id,
            region_id=region_id,
            max_results=max_results,
            max_pages=max_pages,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "data": data}


@router.get("/api/dev/cloud-query-test")
async def run_dev_cloud_query_test(
    run_live: bool = Query(default=False, description="Whether to run live read-only Alibaba Cloud API calls"),
    region_id: str = Query(default="cn-beijing", description="Alibaba Cloud region for live inventory/metric checks"),
    instance_ids: str = Query(default="", description="Optional comma-separated ECS instance IDs for live metric checks"),
    resource_group_id: str = Query(default="", description="Optional resource group ID for live Resource Center checks"),
):
    """Run cloud-query mapping checks and optional live read-only query scenarios."""
    return run_cloud_query_test_suite(
        run_live=run_live,
        region_id=region_id,
        instance_ids=instance_ids,
        resource_group_id=resource_group_id,
    )
