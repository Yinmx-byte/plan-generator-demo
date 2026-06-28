"""Alibaba Cloud SDK helpers for read-only ECS/VPC resource queries."""

from __future__ import annotations

import json
import os
import time
from typing import Any


class AliyunCloudSDKError(RuntimeError):
    """Raised when an Alibaba Cloud SDK call fails."""


def _credentials() -> tuple[str, str, str | None]:
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    security_token = os.getenv("ALIBABA_CLOUD_SECURITY_TOKEN")
    if not access_key_id or not access_key_secret:
        raise AliyunCloudSDKError(
            "请配置 ALIBABA_CLOUD_ACCESS_KEY_ID 和 ALIBABA_CLOUD_ACCESS_KEY_SECRET"
        )
    return access_key_id, access_key_secret, security_token


def _build_config(endpoint: str, region_id: str):
    try:
        from alibabacloud_tea_openapi.models import Config
    except ModuleNotFoundError as exc:
        raise AliyunCloudSDKError(
            "缺少阿里云 SDK 依赖，请在 backend 环境执行 pip install -r requirements.txt"
        ) from exc

    access_key_id, access_key_secret, security_token = _credentials()
    config = Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        security_token=security_token,
        region_id=region_id,
    )
    config.endpoint = endpoint
    config.connect_timeout = int(os.getenv("ALIYUN_OPENAPI_CONNECT_TIMEOUT", "10000"))
    config.read_timeout = int(os.getenv("ALIYUN_OPENAPI_READ_TIMEOUT", "20000"))
    return config


def _ecs_endpoint(region_id: str) -> str:
    return os.getenv("ALIYUN_ECS_ENDPOINT") or f"ecs.{region_id}.aliyuncs.com"


def _vpc_endpoint(region_id: str) -> str:
    return os.getenv("ALIYUN_VPC_ENDPOINT") or f"vpc.{region_id}.aliyuncs.com"


def _cms_endpoint(region_id: str) -> str:
    return os.getenv("ALIYUN_CMS_ENDPOINT") or f"metrics.{region_id}.aliyuncs.com"


def _ecs_client(region_id: str):
    try:
        from alibabacloud_ecs20140526.client import Client
    except ModuleNotFoundError as exc:
        raise AliyunCloudSDKError(
            "缺少 alibabacloud-ecs20140526，请在 backend 环境执行 pip install -r requirements.txt"
        ) from exc
    return Client(_build_config(_ecs_endpoint(region_id), region_id))


def _vpc_client(region_id: str):
    try:
        from alibabacloud_vpc20160428.client import Client
    except ModuleNotFoundError as exc:
        raise AliyunCloudSDKError(
            "缺少 alibabacloud-vpc20160428，请在 backend 环境执行 pip install -r requirements.txt"
        ) from exc
    return Client(_build_config(_vpc_endpoint(region_id), region_id))


def _cms_client(region_id: str):
    try:
        from alibabacloud_cms20190101.client import Client
    except ModuleNotFoundError as exc:
        raise AliyunCloudSDKError(
            "缺少 alibabacloud-cms20190101，请在 backend 环境执行 pip install -r requirements.txt"
        ) from exc
    return Client(_build_config(_cms_endpoint(region_id), region_id))


def _body_to_dict(response: Any) -> dict[str, Any]:
    body = getattr(response, "body", response)
    if hasattr(body, "to_map"):
        return body.to_map()
    if isinstance(body, dict):
        return body
    if hasattr(body, "__dict__"):
        return {
            key: value
            for key, value in vars(body).items()
            if not key.startswith("_") and value is not None
        }
    return {}


def _as_list(value: Any, key: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return [nested]
    return []


def _call_client(client: Any, method_names: tuple[str, ...], request: Any) -> dict[str, Any]:
    for method_name in method_names:
        method = getattr(client, method_name, None)
        if method:
            return _body_to_dict(method(request))
    raise AliyunCloudSDKError(
        f"当前 SDK Client 缺少方法：{', '.join(method_names)}，请检查 SDK 版本。"
    )


def _parse_instance_ids(instance_ids: str | list[str] | None) -> list[str]:
    if not instance_ids:
        return []
    if isinstance(instance_ids, list):
        return [item.strip() for item in instance_ids if item and item.strip()]
    value = instance_ids.strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("instance_ids JSON 必须是数组")
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_metric_names(metric_names: str | list[str] | None) -> list[str]:
    if not metric_names:
        return []
    if isinstance(metric_names, list):
        return [item.strip() for item in metric_names if item and item.strip()]
    return [item.strip() for item in metric_names.split(",") if item.strip()]


def _metric_value(point: dict[str, Any]) -> float | None:
    for key in ("Average", "Value", "Maximum", "Minimum"):
        value = point.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _summarize_datapoints(datapoints: list[dict[str, Any]]) -> dict[str, Any]:
    if not datapoints:
        return {"available": False, "count": 0, "latest": None, "average": None, "maximum": None, "minimum": None}
    sorted_points = sorted(datapoints, key=lambda item: item.get("timestamp", 0))
    values = [value for value in (_metric_value(point) for point in sorted_points) if value is not None]
    latest_point = sorted_points[-1]
    latest_value = _metric_value(latest_point)
    return {
        "available": bool(values),
        "count": len(sorted_points),
        "latest": latest_value,
        "latest_timestamp": latest_point.get("timestamp"),
        "average": round(sum(values) / len(values), 4) if values else None,
        "maximum": max(values) if values else None,
        "minimum": min(values) if values else None,
        "recent_points": sorted_points[-5:],
    }


def describe_regions(region_id: str) -> list[dict[str, Any]]:
    from alibabacloud_ecs20140526 import models as ecs_models

    request = ecs_models.DescribeRegionsRequest(accept_language="zh-CN")
    payload = _call_client(
        _ecs_client(region_id),
        ("describe_regions", "describe_regions_with_options"),
        request,
    )
    return _as_list(payload.get("Regions"), "Region")


def describe_vpcs(
    *,
    region_id: str,
    vpc_id: str = "",
    vpc_name: str = "",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    from alibabacloud_vpc20160428 import models as vpc_models

    request = vpc_models.DescribeVpcsRequest(
        region_id=region_id,
        vpc_id=vpc_id or None,
        vpc_name=vpc_name or None,
        page_number=1,
        page_size=page_size,
    )
    payload = _call_client(
        _vpc_client(region_id),
        ("describe_vpcs", "describe_vpcs_with_options"),
        request,
    )
    return _as_list(payload.get("Vpcs"), "Vpc")


def describe_vswitches(
    *,
    region_id: str,
    vpc_id: str = "",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    from alibabacloud_vpc20160428 import models as vpc_models

    request = vpc_models.DescribeVSwitchesRequest(
        region_id=region_id,
        vpc_id=vpc_id or None,
        page_number=1,
        page_size=page_size,
    )
    payload = _call_client(
        _vpc_client(region_id),
        (
            "describe_v_switches",
            "describe_vswitches",
            "describe_v_switches_with_options",
            "describe_vswitches_with_options",
        ),
        request,
    )
    return _as_list(payload.get("VSwitches"), "VSwitch")


def describe_instances(
    *,
    region_id: str,
    vpc_id: str = "",
    vswitch_id: str = "",
    instance_ids: str | list[str] | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    from alibabacloud_ecs20140526 import models as ecs_models

    parsed_instance_ids = _parse_instance_ids(instance_ids)
    request = ecs_models.DescribeInstancesRequest(
        region_id=region_id,
        vpc_id=vpc_id or None,
        v_switch_id=vswitch_id or None,
        instance_ids=json.dumps(parsed_instance_ids) if parsed_instance_ids else None,
        page_number=1,
        page_size=page_size,
    )
    payload = _call_client(
        _ecs_client(region_id),
        ("describe_instances", "describe_instances_with_options"),
        request,
    )
    return _as_list(payload.get("Instances"), "Instance")


def describe_metric_list(
    *,
    region_id: str,
    metric_name: str,
    dimensions: dict[str, Any],
    namespace: str = "acs_ecs_dashboard",
    period: str = "300",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
) -> list[dict[str, Any]]:
    from alibabacloud_cms20190101 import models as cms_models

    request = cms_models.DescribeMetricListRequest(
        namespace=namespace,
        metric_name=metric_name,
        dimensions=json.dumps([dimensions], ensure_ascii=False),
        period=str(period),
        start_time=str(start_time_ms) if start_time_ms else None,
        end_time=str(end_time_ms) if end_time_ms else None,
    )
    payload = _call_client(
        _cms_client(region_id),
        ("describe_metric_list", "describe_metric_list_with_options"),
        request,
    )
    datapoints = payload.get("Datapoints") or payload.get("DataPoints") or []
    if isinstance(datapoints, str):
        try:
            parsed = json.loads(datapoints)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return datapoints if isinstance(datapoints, list) else []


def describe_instance_usage_metrics(
    *,
    region_id: str,
    instance_id: str,
    metric_names: str | list[str],
    metric_minutes: int = 60,
    period: str = "300",
) -> dict[str, Any]:
    end_time_ms = int(time.time() * 1000)
    start_time_ms = end_time_ms - max(metric_minutes, 1) * 60 * 1000
    summaries = {}
    for metric_name in _parse_metric_names(metric_names):
        datapoints = describe_metric_list(
            region_id=region_id,
            metric_name=metric_name,
            dimensions={"instanceId": instance_id},
            period=period,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )
        summaries[metric_name] = _summarize_datapoints(datapoints)
    return {
        "instance_id": instance_id,
        "namespace": "acs_ecs_dashboard",
        "period": str(period),
        "window_minutes": metric_minutes,
        "metrics": summaries,
    }


def build_usage_summary(
    *,
    vpcs: list[dict[str, Any]],
    vswitches: list[dict[str, Any]],
    instances: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    total_cpu = 0
    total_memory_mb = 0
    vpc_instance_counts: dict[str, int] = {}
    for instance in instances:
        status = str(instance.get("Status") or "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        total_cpu += int(instance.get("Cpu") or 0)
        total_memory_mb += int(instance.get("Memory") or 0)
        vpc_id = ((instance.get("VpcAttributes") or {}).get("VpcId")) or ""
        if vpc_id:
            vpc_instance_counts[vpc_id] = vpc_instance_counts.get(vpc_id, 0) + 1

    vswitch_usage = []
    total_available_ip = 0
    for vswitch in vswitches:
        available = int(vswitch.get("AvailableIpAddressCount") or 0)
        total_available_ip += available
        vswitch_usage.append(
            {
                "vpc_id": vswitch.get("VpcId"),
                "vswitch_id": vswitch.get("VSwitchId"),
                "zone_id": vswitch.get("ZoneId"),
                "cidr_block": vswitch.get("CidrBlock"),
                "status": vswitch.get("Status"),
                "available_ip_address_count": available,
            }
        )

    return {
        "ecs": {
            "total_instances": len(instances),
            "status_counts": status_counts,
            "total_cpu_cores": total_cpu,
            "total_memory_mb": total_memory_mb,
            "total_memory_gb": round(total_memory_mb / 1024, 2) if total_memory_mb else 0,
            "instances_by_vpc": vpc_instance_counts,
        },
        "vpc": {
            "total_vpcs": len(vpcs),
            "total_vswitches": len(vswitches),
            "total_available_ip_address_count": total_available_ip,
            "vswitch_usage": vswitch_usage,
        },
    }


def query_ecs_vpc_info(
    *,
    region_id: str = "cn-beijing",
    vpc_id: str = "",
    vpc_name: str = "",
    instance_ids: str | list[str] | None = None,
    include_instances: bool = True,
    include_vswitches: bool = True,
    include_usage_metrics: bool = True,
    metric_names: str | list[str] | None = None,
    metric_minutes: int = 60,
    metric_period: str = "300",
) -> dict[str, Any]:
    """Query read-only ECS/VPC topology information for a region or VPC."""
    if not region_id.strip():
        raise ValueError("region_id 不能为空")
    page_size = int(os.getenv("ALIYUN_CLOUD_QUERY_PAGE_SIZE", "50"))
    max_vpcs = int(os.getenv("ALIYUN_CLOUD_QUERY_MAX_VPCS", "10"))

    regions = describe_regions(region_id)
    vpcs = describe_vpcs(
        region_id=region_id,
        vpc_id=vpc_id,
        vpc_name=vpc_name,
        page_size=page_size,
    )
    selected_vpc_ids = [item.get("VpcId", "") for item in vpcs[:max_vpcs] if item.get("VpcId")]
    if vpc_id and vpc_id not in selected_vpc_ids:
        selected_vpc_ids.append(vpc_id)

    vswitches: list[dict[str, Any]] = []
    if include_vswitches:
        if selected_vpc_ids:
            for current_vpc_id in selected_vpc_ids:
                vswitches.extend(
                    describe_vswitches(
                        region_id=region_id,
                        vpc_id=current_vpc_id,
                        page_size=page_size,
                    )
                )
        else:
            vswitches = describe_vswitches(region_id=region_id, page_size=page_size)

    instances: list[dict[str, Any]] = []
    if include_instances:
        parsed_instance_ids = _parse_instance_ids(instance_ids)
        if parsed_instance_ids:
            instances = describe_instances(
                region_id=region_id,
                instance_ids=parsed_instance_ids,
                page_size=page_size,
            )
        elif selected_vpc_ids:
            for current_vpc_id in selected_vpc_ids:
                instances.extend(
                    describe_instances(
                        region_id=region_id,
                        vpc_id=current_vpc_id,
                        page_size=page_size,
                    )
                )

    effective_metric_names = metric_names or os.getenv(
        "ALIYUN_ECS_USAGE_METRICS",
        "CPUUtilization",
    )
    metric_errors = []
    usage_metrics = {}
    if include_usage_metrics and instances:
        for instance in instances:
            instance_id = instance.get("InstanceId")
            if not instance_id:
                continue
            try:
                instance_usage = describe_instance_usage_metrics(
                    region_id=region_id,
                    instance_id=instance_id,
                    metric_names=effective_metric_names,
                    metric_minutes=metric_minutes,
                    period=metric_period,
                )
                usage_metrics[instance_id] = instance_usage
                instance["UsageMetrics"] = instance_usage
            except Exception as exc:
                metric_errors.append({"instance_id": instance_id, "message": str(exc)})

    return {
        "region_id": region_id,
        "regions": regions,
        "vpcs": vpcs,
        "vswitches": vswitches,
        "instances": instances,
        "usage_summary": build_usage_summary(vpcs=vpcs, vswitches=vswitches, instances=instances),
        "usage_metrics": usage_metrics,
        "metric_errors": metric_errors,
        "counts": {
            "regions": len(regions),
            "vpcs": len(vpcs),
            "vswitches": len(vswitches),
            "instances": len(instances),
        },
        "filters": {
            "vpc_id": vpc_id,
            "vpc_name": vpc_name,
            "instance_ids": _parse_instance_ids(instance_ids),
            "include_instances": include_instances,
            "include_vswitches": include_vswitches,
            "include_usage_metrics": include_usage_metrics,
            "metric_names": _parse_metric_names(effective_metric_names),
            "metric_minutes": metric_minutes,
            "metric_period": str(metric_period),
        },
        "sdk": {
            "ecs": "alibabacloud-ecs20140526",
            "vpc": "alibabacloud-vpc20160428",
            "cms": "alibabacloud-cms20190101",
        },
    }
