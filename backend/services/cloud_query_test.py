"""Development checks for config-driven cloud query behavior."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from services.aliyun_cloud_sdk import (
    describe_configured_instance_metric,
    query_cloud_inventory,
    query_cloud_metrics,
    query_resource_group_products,
    summarize_resource_center_products,
)
from services.cloud_query_catalog import (
    load_cloud_query_catalog,
    resolve_metric,
    resolve_product,
)


def _scenario(name: str, func: Callable[[], Any]) -> dict[str, Any]:
    try:
        data = func()
    except Exception as exc:
        return {
            "name": name,
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    return {
        "name": name,
        "status": "passed",
        "data": data,
    }


def _credential_status() -> dict[str, bool]:
    return {
        "access_key_id": bool(os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")),
        "access_key_secret": bool(os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")),
        "security_token": bool(os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN")),
    }


def _test_product_aliases() -> dict[str, Any]:
    catalog = load_cloud_query_catalog()
    resolved = {}
    for product_key, config in catalog.get("products", {}).items():
        samples = [product_key, config.get("product_code", ""), *(config.get("aliases") or [])[:2]]
        resolved[product_key] = [resolve_product(alias)[0] for alias in samples if alias]
    return resolved


def _test_ecs_metric_aliases() -> dict[str, Any]:
    metric_cases = {
        "CPU使用率": "cpu_usage",
        "内存使用率": "memory_usage",
        "磁盘使用率": "disk_usage",
        "公网出带宽": "public_network_out",
    }
    return {
        alias: {
            "resolved_key": resolve_metric("ecs", alias)[0],
            "expected_key": expected,
            "matched": resolve_metric("ecs", alias)[0] == expected,
        }
        for alias, expected in metric_cases.items()
    }


def _test_disk_metric_needs_device() -> dict[str, Any]:
    result = describe_configured_instance_metric(
        region_id="cn-beijing",
        instance_id="i-test",
        metric="磁盘使用率",
    )
    return {
        "status": result.get("status"),
        "missing_dimensions": result.get("missing_dimensions", []),
        "notes": result.get("notes", ""),
    }


def _test_resource_product_summary() -> dict[str, Any]:
    resources = [
        {"ResourceType": "ACS::ECS::Instance", "ResourceGroupId": "rg-a"},
        {"ResourceType": "ACS::ECS::Disk", "ResourceGroupId": "rg-a"},
        {"ResourceType": "ACS::VPC::VPC", "ResourceGroupId": "rg-a"},
        {"ResourceType": "ACS::OSS::Bucket", "ResourceGroupId": "rg-b"},
        {"ResourceType": "ACS::SLB::LoadBalancer", "ResourceGroupId": "rg-b"},
    ]
    return summarize_resource_center_products(resources)


def _test_metric_requires_instance_ids() -> dict[str, Any]:
    try:
        query_cloud_metrics(product="ecs", metric="cpu_usage", instance_ids="")
    except ValueError as exc:
        return {"status": "need_more", "message": str(exc)}
    raise AssertionError("query_cloud_metrics should require instance_ids")


def _compact_inventory(data: dict[str, Any]) -> dict[str, Any]:
    usage_summary = data.get("usage_summary", {})
    ecs_summary = usage_summary.get("ecs", {})
    vpc_summary = usage_summary.get("vpc", {})
    return {
        "region_id": data.get("region_id"),
        "counts": data.get("counts", {}),
        "running_instances": (ecs_summary.get("status_counts") or {}).get("Running", 0),
        "total_cpu_cores": ecs_summary.get("total_cpu_cores"),
        "total_memory_gb": ecs_summary.get("total_memory_gb"),
        "total_available_ip_address_count": vpc_summary.get("total_available_ip_address_count"),
    }


def _compact_resource_products(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_count": data.get("product_count"),
        "total_resource_count": data.get("total_resource_count"),
        "products": [
            {
                "product_code": item.get("product_code"),
                "product_name": item.get("product_name"),
                "resource_count": item.get("resource_count"),
                "resource_types": item.get("resource_types", {}),
            }
            for item in data.get("products", [])
        ],
        "resource_group_counts": data.get("resource_group_counts", {}),
        "notes": data.get("notes", ""),
    }


def _compact_metric(data: dict[str, Any]) -> dict[str, Any]:
    metrics = data.get("metrics", [])
    compact_metrics = []
    for item in metrics:
        summary = dict(item.get("summary") or {})
        summary.pop("recent_points", None)
        compact_metrics.append(
            {
                "instance_id": item.get("instance_id"),
                "status": item.get("status"),
                "missing_dimensions": item.get("missing_dimensions"),
                "summary": summary,
                "notes": item.get("notes", ""),
            }
        )
    return {
        "region_id": data.get("region_id"),
        "product": data.get("product", {}),
        "metric": data.get("metric", {}),
        "filters": data.get("filters", {}),
        "metrics": compact_metrics,
    }


def _live_scenarios(
    *,
    region_id: str,
    instance_ids: str,
    resource_group_id: str,
) -> list[dict[str, Any]]:
    if not all(_credential_status()[key] for key in ("access_key_id", "access_key_secret")):
        return [
            {
                "name": "live_cloud_queries",
                "status": "skipped",
                "reason": "ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET are not configured in current process.",
            }
        ]

    scenarios = [
        _scenario(
            "live_ecs_inventory",
            lambda: _compact_inventory(
                query_cloud_inventory(
                    product="ecs",
                    region_id=region_id,
                    include_instances=True,
                    include_vswitches=True,
                )
            ),
        ),
        _scenario(
            "live_resource_group_products",
            lambda: _compact_resource_products(
                query_resource_group_products(resource_group_id=resource_group_id)
            ),
        ),
    ]
    if instance_ids.strip():
        scenarios.append(
            _scenario(
                "live_ecs_cpu_metric",
                lambda: _compact_metric(
                    query_cloud_metrics(
                        product="ecs",
                        metric="cpu_usage",
                        region_id=region_id,
                        instance_ids=instance_ids,
                    )
                ),
            )
        )
    else:
        scenarios.append(
            {
                "name": "live_ecs_cpu_metric",
                "status": "skipped",
                "reason": "instance_ids is empty.",
            }
        )
    return scenarios


def run_cloud_query_test_suite(
    *,
    run_live: bool = False,
    region_id: str = "cn-beijing",
    instance_ids: str = "",
    resource_group_id: str = "",
) -> dict[str, Any]:
    """Run deterministic cloud-query checks and optional live read-only calls."""
    deterministic = [
        _scenario("catalog_product_aliases", _test_product_aliases),
        _scenario("ecs_metric_aliases", _test_ecs_metric_aliases),
        _scenario("disk_metric_requires_device", _test_disk_metric_needs_device),
        _scenario("resource_product_summary", _test_resource_product_summary),
        _scenario("metric_requires_instance_ids", _test_metric_requires_instance_ids),
    ]
    live = (
        _live_scenarios(
            region_id=region_id,
            instance_ids=instance_ids,
            resource_group_id=resource_group_id,
        )
        if run_live
        else [{"name": "live_cloud_queries", "status": "skipped", "reason": "run_live=false"}]
    )
    scenarios = deterministic + live
    failed = [item for item in scenarios if item["status"] == "failed"]
    return {
        "status": "passed" if not failed else "failed",
        "credentials": _credential_status(),
        "parameters": {
            "run_live": run_live,
            "region_id": region_id,
            "instance_ids": instance_ids,
            "resource_group_id": resource_group_id,
        },
        "summary": {
            "total": len(scenarios),
            "passed": len([item for item in scenarios if item["status"] == "passed"]),
            "skipped": len([item for item in scenarios if item["status"] == "skipped"]),
            "failed": len(failed),
        },
        "scenarios": scenarios,
    }
