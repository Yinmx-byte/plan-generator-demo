"""Alibaba Cloud SDK helpers for read-only ECS/VPC resource queries."""

from __future__ import annotations

import json
import os
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


def query_ecs_vpc_info(
    *,
    region_id: str = "cn-beijing",
    vpc_id: str = "",
    vpc_name: str = "",
    instance_ids: str | list[str] | None = None,
    include_instances: bool = True,
    include_vswitches: bool = True,
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

    return {
        "region_id": region_id,
        "regions": regions,
        "vpcs": vpcs,
        "vswitches": vswitches,
        "instances": instances,
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
        },
        "sdk": {
            "ecs": "alibabacloud-ecs20140526",
            "vpc": "alibabacloud-vpc20160428",
        },
    }
