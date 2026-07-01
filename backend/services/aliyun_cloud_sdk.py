"""Alibaba Cloud SDK helpers for read-only ECS/VPC resource queries."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from services.cloud_query_catalog import (
    get_catalog_defaults,
    product_code_from_resource_type,
    product_name_for_code,
    resolve_metric,
    resolve_product,
)


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


def _resource_center_endpoint() -> str:
    return os.getenv("ALIYUN_RESOURCE_CENTER_ENDPOINT") or "resourcecenter.aliyuncs.com"


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


def _resource_center_client(region_id: str):
    try:
        from alibabacloud_resourcecenter20221201.client import Client
    except ModuleNotFoundError as exc:
        raise AliyunCloudSDKError(
            "缺少 alibabacloud-resourcecenter20221201，请在 backend 环境执行 pip install -r requirements.txt"
        ) from exc
    return Client(_build_config(_resource_center_endpoint(), region_id))


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


def _is_metric_overview_request(metric: str | None) -> bool:
    normalized = (metric or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    return normalized in {
        "",
        "all",
        "overview",
        "usageoverview",
        "resourceusage",
        "资源占用",
        "全部",
        "全部指标",
        "所有指标",
    }


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


def _build_metric_no_data_diagnosis(
    *,
    metric_key: str,
    metric_config: dict[str, Any],
    dimensions: dict[str, Any],
    window_minutes: int,
    attempted_metric_names: list[str] | None = None,
    attempted_dimensions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    display_name = metric_config.get("display_name") or metric_key
    possible_causes = [
        f"云监控在最近 {window_minutes} 分钟内未返回 {display_name} 采样点",
        "实例 ID、地域或账号权限与实际资源不匹配",
        "该指标在当前实例、镜像或监控命名空间下暂未上报",
    ]
    next_steps = [
        "确认实例地域、实例 ID 和当前后端 AK/SK 所属账号是否一致",
        "扩大查询时间范围后重试，例如最近 6 小时或 24 小时",
    ]
    if metric_key == "memory_usage":
        possible_causes.append("内存指标通常依赖云监控插件或增强监控采集，插件未运行时可能无数据")
        next_steps.append("在 ECS 控制台或云监控控制台确认云监控插件/增强监控状态")
    if metric_key == "disk_usage":
        possible_causes.append("磁盘指标需要 device 维度，device 值与实例实际设备名不一致时会返回空")
        next_steps.append("确认实例内实际设备名或挂载点，例如 /dev/vda1、/dev/vdb1")
    if dimensions.get("device"):
        next_steps.append(f"当前查询使用 device={dimensions['device']}，可换成实例实际设备名重试")
    if attempted_metric_names:
        next_steps.append(f"本次已尝试指标：{', '.join(attempted_metric_names)}")
    if attempted_dimensions:
        devices = [str(item.get("device")) for item in attempted_dimensions if item.get("device")]
        if devices:
            next_steps.append(f"本次已尝试设备维度：{', '.join(dict.fromkeys(devices))}")
    return {
        "code": "no_datapoints",
        "message": "CloudMonitor 接口返回成功，但没有返回该指标的采样点，不能据此得出实际使用率。",
        "possible_causes": possible_causes,
        "next_steps": next_steps,
    }


def _resource_matches_product(resource: dict[str, Any], product_config: dict[str, Any]) -> bool:
    resource_type = str(resource.get("ResourceType") or "")
    prefixes = [str(item) for item in product_config.get("resource_type_prefixes", []) if item]
    if prefixes:
        return any(resource_type.startswith(prefix) for prefix in prefixes)
    product_code = str(product_config.get("product_code") or "").upper()
    return bool(product_code and f"::{product_code}::" in resource_type.upper())


def _resource_matches_region(resource: dict[str, Any], region_id: str) -> bool:
    resource_region = str(resource.get("RegionId") or "")
    return not resource_region or not region_id or resource_region == region_id


def _generic_inventory_summary(resources: list[dict[str, Any]]) -> dict[str, Any]:
    by_region: dict[str, int] = {}
    by_resource_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_resource_group: dict[str, int] = {}
    for resource in resources:
        region = str(resource.get("RegionId") or "unknown")
        resource_type = str(resource.get("ResourceType") or "unknown")
        status = str(resource.get("ResourceStatus") or resource.get("Status") or "unknown")
        resource_group_id = str(resource.get("ResourceGroupId") or "unknown")
        by_region[region] = by_region.get(region, 0) + 1
        by_resource_type[resource_type] = by_resource_type.get(resource_type, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_resource_group[resource_group_id] = by_resource_group.get(resource_group_id, 0) + 1
    return {
        "total_resources": len(resources),
        "by_region": by_region,
        "by_resource_type": by_resource_type,
        "by_status": by_status,
        "by_resource_group": by_resource_group,
    }


def query_generic_product_inventory(
    *,
    product_key: str,
    product_config: dict[str, Any],
    region_id: str,
    resource_ids: str | list[str] | None = None,
) -> dict[str, Any]:
    """Query product resources through Resource Center for products without a dedicated adapter."""
    wanted_ids = set(_parse_instance_ids(resource_ids))
    resources = [
        resource
        for resource in search_resource_center_resources()
        if _resource_matches_product(resource, product_config)
        and _resource_matches_region(resource, region_id)
    ]
    if wanted_ids:
        resources = [
            resource
            for resource in resources
            if str(resource.get("ResourceId") or "") in wanted_ids
            or str(resource.get("ResourceName") or "") in wanted_ids
        ]
    return {
        "query_type": "inventory",
        "region_id": region_id,
        "inventory_provider": "resource_center",
        "product": {
            "key": product_key,
            "code": product_config.get("product_code"),
            "name": product_config.get("product_name"),
        },
        "filters": {"resource_ids": sorted(wanted_ids)},
        "resources": resources,
        "summary": _generic_inventory_summary(resources),
        "sdk": {"resourcecenter": "alibabacloud-resourcecenter20221201"},
        "notes": "Resource Center inventory is generic and read-only; detailed product attributes depend on Resource Center visibility.",
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


def describe_disks(
    *,
    region_id: str,
    instance_id: str = "",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    from alibabacloud_ecs20140526 import models as ecs_models

    request = ecs_models.DescribeDisksRequest(
        region_id=region_id,
        instance_id=instance_id or None,
        page_number=1,
        page_size=page_size,
    )
    payload = _call_client(
        _ecs_client(region_id),
        ("describe_disks", "describe_disks_with_options"),
        request,
    )
    return _as_list(payload.get("Disks"), "Disk")


def _disk_device_candidates(disks: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for disk in disks:
        attachments = _as_list((disk.get("Attachments") or {}), "Attachment")
        for device in [disk.get("Device"), *[item.get("Device") for item in attachments]]:
            if not device:
                continue
            value = str(device)
            candidates.append(value)
            if value.startswith("/dev/") and not value[-1:].isdigit():
                candidates.append(f"{value}1")
    return list(dict.fromkeys(candidates))


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


def describe_configured_instance_metric(
    *,
    region_id: str,
    instance_id: str,
    product: str = "ecs",
    metric: str = "cpu_usage",
    metric_minutes: int | None = None,
    period: str | None = None,
    extra_dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metric_key, metric_config = resolve_metric(product, metric)
    dimension_names = [str(item) for item in metric_config.get("dimensions", []) if item]
    resource_dimension = str(metric_config.get("resource_id_dimension") or (dimension_names[0] if dimension_names else "instanceId"))
    dimensions = {resource_dimension: instance_id}
    dimensions.update(metric_config.get("default_dimensions") or {})
    dimensions.update(extra_dimensions or {})
    missing_dimensions = [
        item for item in (metric_config.get("required_user_dimensions") or []) if not dimensions.get(item)
    ]
    auto_dimensions: list[dict[str, Any]] = []
    if product == "ecs" and missing_dimensions == ["device"] and metric_key == "disk_usage":
        for device in _disk_device_candidates(describe_disks(region_id=region_id, instance_id=instance_id)):
            candidate = dict(dimensions)
            candidate["device"] = device
            auto_dimensions.append(candidate)
        if auto_dimensions:
            missing_dimensions = []
    if missing_dimensions:
        return {
            "instance_id": instance_id,
            "metric_key": metric_key,
            "display_name": metric_config.get("display_name", metric_key),
            "metric_name": metric_config.get("metric_name"),
            "namespace": metric_config.get("namespace", "acs_ecs_dashboard"),
            "status": "need_more",
            "missing_dimensions": missing_dimensions,
            "message": "Metric requires additional dimensions.",
            "notes": metric_config.get("notes", ""),
        }

    defaults = get_catalog_defaults()
    effective_minutes = int(metric_minutes or metric_config.get("default_minutes") or defaults.get("metric_minutes", 60))
    effective_period = str(period or metric_config.get("default_period") or defaults.get("metric_period", "300"))
    end_time_ms = int(time.time() * 1000)
    start_time_ms = end_time_ms - max(effective_minutes, 1) * 60 * 1000
    metric_names = [
        str(metric_config["metric_name"]),
        *[str(item) for item in metric_config.get("fallback_metric_names", [])],
    ]
    metric_names = list(dict.fromkeys(metric_names))
    dimension_candidates = auto_dimensions or [dimensions]
    attempts = []
    selected_attempt: dict[str, Any] | None = None
    for metric_name in metric_names:
        for candidate_dimensions in dimension_candidates:
            error = ""
            try:
                datapoints = describe_metric_list(
                    region_id=region_id,
                    metric_name=metric_name,
                    dimensions=candidate_dimensions,
                    namespace=str(metric_config.get("namespace", "acs_ecs_dashboard")),
                    period=effective_period,
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                )
                summary = _summarize_datapoints(datapoints)
            except Exception as exc:
                error = str(exc)
                summary = _summarize_datapoints([])
            attempt = {
                "metric_name": metric_name,
                "dimensions": candidate_dimensions,
                "summary": summary,
                "error": error,
            }
            attempts.append(attempt)
            if summary.get("available") and selected_attempt is None:
                selected_attempt = attempt
                break
        if selected_attempt is not None:
            break
    selected_attempt = selected_attempt or (attempts[0] if attempts else None)
    summary = dict((selected_attempt or {}).get("summary") or _summarize_datapoints([]))
    selected_dimensions = dict((selected_attempt or {}).get("dimensions") or dimensions)
    selected_metric_name = str((selected_attempt or {}).get("metric_name") or metric_config["metric_name"])
    diagnosis = None
    status = "ok"
    message = ""
    if not summary.get("available"):
        status = "no_data"
        diagnosis = _build_metric_no_data_diagnosis(
            metric_key=metric_key,
            metric_config=metric_config,
            dimensions=selected_dimensions,
            window_minutes=effective_minutes,
            attempted_metric_names=metric_names,
            attempted_dimensions=[dict(item["dimensions"]) for item in attempts],
        )
        message = diagnosis["message"]
    return {
        "instance_id": instance_id,
        "metric_key": metric_key,
        "display_name": metric_config.get("display_name", metric_key),
        "metric_name": selected_metric_name,
        "configured_metric_name": metric_config.get("metric_name"),
        "namespace": metric_config.get("namespace", "acs_ecs_dashboard"),
        "dimensions": selected_dimensions,
        "attempts": [
            {
                "metric_name": item["metric_name"],
                "dimensions": item["dimensions"],
                "available": item["summary"].get("available"),
                "count": item["summary"].get("count"),
                "error": item.get("error", ""),
            }
            for item in attempts
        ],
        "period": effective_period,
        "window_minutes": effective_minutes,
        "notes": metric_config.get("notes", ""),
        "status": status,
        "message": message,
        "summary": summary,
        "diagnosis": diagnosis,
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


def query_cloud_inventory(
    *,
    product: str = "ecs",
    region_id: str | None = None,
    vpc_id: str = "",
    vpc_name: str = "",
    instance_ids: str | list[str] | None = None,
    include_instances: bool = True,
    include_vswitches: bool = True,
) -> dict[str, Any]:
    """Query product inventory through a configured adapter or Resource Center fallback."""
    defaults = get_catalog_defaults()
    product_key, product_config = resolve_product(product)
    effective_region_id = region_id or defaults.get("region_id", "cn-beijing")
    inventory_config = product_config.get("inventory") or {}
    provider = inventory_config.get("provider") or product_config.get("inventory_provider")
    if not provider:
        provider = "ecs_vpc" if product_key in {"ecs", "vpc"} else "resource_center"
    if provider == "resource_center":
        return query_generic_product_inventory(
            product_key=product_key,
            product_config=product_config,
            region_id=effective_region_id,
            resource_ids=instance_ids,
        )
    if provider not in {"ecs", "vpc", "ecs_vpc"}:
        raise AliyunCloudSDKError(
            f"Inventory provider is configured but no adapter is registered: {provider}"
        )
    data = query_ecs_vpc_info(
        region_id=effective_region_id,
        vpc_id=vpc_id,
        vpc_name=vpc_name,
        instance_ids=instance_ids,
        include_instances=include_instances,
        include_vswitches=include_vswitches,
        include_usage_metrics=False,
    )
    data["query_type"] = "inventory"
    data["product"] = {
        "key": product_key,
        "code": product_config.get("product_code"),
        "name": product_config.get("product_name"),
    }
    return data


def query_cloud_metrics(
    *,
    product: str = "ecs",
    metric: str = "all",
    region_id: str | None = None,
    instance_ids: str | list[str] | None = None,
    metric_minutes: int | None = None,
    metric_period: str | None = None,
    extra_dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Query metrics by stable product and metric keys resolved from the catalog."""
    defaults = get_catalog_defaults()
    product_key, product_config = resolve_product(product)
    effective_region_id = region_id or defaults.get("region_id", "cn-beijing")
    parsed_resource_ids = _parse_instance_ids(instance_ids)
    if not parsed_resource_ids:
        if product_key == "ecs":
            raise ValueError("instance_ids is required for ECS metric queries")
        inventory = query_generic_product_inventory(
            product_key=product_key,
            product_config=product_config,
            region_id=effective_region_id,
        )
        parsed_resource_ids = [
            str(resource.get("ResourceId") or resource.get("ResourceName") or "")
            for resource in inventory.get("resources", [])
            if str(resource.get("ResourceId") or resource.get("ResourceName") or "").strip()
        ]
    if not parsed_resource_ids:
        raise ValueError(f"resource ids are required or discoverable for {product_key} metric queries")

    if _is_metric_overview_request(metric):
        metric_keys = list(product_config.get("default_metric_set") or [])
        if not metric_keys:
            raise AliyunCloudSDKError(f"No default metrics configured for product: {product_key}")
        metric_config = {
            "display_name": "资源占用概览",
            "metric_name": "overview",
            "namespace": "",
        }
    else:
        metric_key, metric_config = resolve_metric(product_key, metric)
        metric_keys = [metric_key]

    results = []
    resolved_metrics = []
    for metric_key in metric_keys:
        _resolved_key, current_metric_config = resolve_metric(product_key, metric_key)
        resolved_metrics.append(
            {
                "key": _resolved_key,
                "display_name": current_metric_config.get("display_name"),
                "metric_name": current_metric_config.get("metric_name"),
                "namespace": current_metric_config.get("namespace"),
                "notes": current_metric_config.get("notes", ""),
            }
        )
        for resource_id in parsed_resource_ids:
            results.append(
                describe_configured_instance_metric(
                    region_id=effective_region_id,
                    instance_id=resource_id,
                    product=product_key,
                    metric=_resolved_key,
                    metric_minutes=metric_minutes,
                    period=metric_period,
                    extra_dimensions=extra_dimensions,
                )
            )
    return {
        "query_type": "metric",
        "region_id": effective_region_id,
        "product": {
            "key": product_key,
            "code": product_config.get("product_code"),
            "name": product_config.get("product_name"),
        },
        "metric": {
            "key": "overview" if len(metric_keys) > 1 else resolved_metrics[0]["key"],
            "display_name": metric_config.get("display_name"),
            "metric_name": metric_config.get("metric_name"),
            "namespace": metric_config.get("namespace"),
            "notes": metric_config.get("notes", ""),
            "resolved_metrics": resolved_metrics,
        },
        "filters": {
            "instance_ids": parsed_resource_ids,
            "resource_ids": parsed_resource_ids,
            "metric_minutes": metric_minutes or defaults.get("metric_minutes"),
            "metric_period": str(metric_period or defaults.get("metric_period")),
            "extra_dimensions": extra_dimensions or {},
        },
        "metrics": results,
    }


def search_resource_center_resources(
    *,
    resource_group_id: str = "",
    region_id: str | None = None,
    max_results: int | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    """Search Resource Center resources accessible to the current account."""
    from alibabacloud_resourcecenter20221201 import models as rc_models

    defaults = get_catalog_defaults()
    effective_region_id = (
        region_id
        or os.getenv("ALIYUN_RESOURCE_CENTER_REGION_ID")
        or defaults.get("resource_center_region_id", "cn-hangzhou")
    )
    page_size = int(max_results or defaults.get("resource_center_max_results", 100))
    page_limit = int(max_pages or defaults.get("resource_center_max_pages", 20))
    client = _resource_center_client(effective_region_id)
    resources: list[dict[str, Any]] = []
    next_token = ""
    for _page in range(max(page_limit, 1)):
        request = rc_models.SearchResourcesRequest(
            max_results=page_size,
            next_token=next_token or None,
            resource_group_id=resource_group_id or None,
            include_deleted_resources=False,
        )
        payload = _call_client(
            client,
            ("search_resources", "search_resources_with_options"),
            request,
        )
        resources.extend(_as_list(payload.get("Resources"), "Resource"))
        next_token = payload.get("NextToken") or payload.get("next_token") or ""
        if not next_token:
            break
    return resources


def summarize_resource_center_products(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize Resource Center resources by product code."""
    product_counts: dict[str, dict[str, Any]] = {}
    resource_group_counts: dict[str, int] = {}
    for resource in resources:
        resource_type = str(resource.get("ResourceType") or "")
        product_code = product_code_from_resource_type(resource_type)
        item = product_counts.setdefault(
            product_code,
            {
                "product_code": product_code,
                "product_name": product_name_for_code(product_code),
                "resource_count": 0,
                "resource_types": {},
            },
        )
        item["resource_count"] += 1
        item["resource_types"][resource_type] = item["resource_types"].get(resource_type, 0) + 1
        current_resource_group_id = str(resource.get("ResourceGroupId") or "")
        if current_resource_group_id:
            resource_group_counts[current_resource_group_id] = resource_group_counts.get(current_resource_group_id, 0) + 1

    products = sorted(
        product_counts.values(),
        key=lambda item: (-int(item["resource_count"]), str(item["product_code"])),
    )
    return {
        "product_count": len(products),
        "total_resource_count": len(resources),
        "products": products,
        "resource_group_counts": resource_group_counts,
    }


def query_resource_group_products(
    *,
    resource_group_id: str = "",
    region_id: str | None = None,
    max_results: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Count product categories in accessible Resource Center resources."""
    defaults = get_catalog_defaults()
    effective_region_id = (
        region_id
        or os.getenv("ALIYUN_RESOURCE_CENTER_REGION_ID")
        or defaults.get("resource_center_region_id", "cn-hangzhou")
    )
    resources = search_resource_center_resources(
        resource_group_id=resource_group_id,
        region_id=effective_region_id,
        max_results=max_results,
        max_pages=max_pages,
    )
    summary = summarize_resource_center_products(resources)
    return {
        "query_type": "resource_group_products",
        "region_id": effective_region_id,
        "resource_group_id": resource_group_id,
        **summary,
        "sample_resources": resources[:10],
        "limits": {
            "max_results": int(max_results or defaults.get("resource_center_max_results", 100)),
            "max_pages": int(max_pages or defaults.get("resource_center_max_pages", 20)),
        },
        "sdk": {
            "resourcecenter": "alibabacloud-resourcecenter20221201",
        },
        "notes": "Resource Center returns resources that are visible to the current account and supported by Resource Center.",
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
