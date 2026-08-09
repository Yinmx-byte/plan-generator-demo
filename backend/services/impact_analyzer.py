"""Impact analysis engine for cloud resource maintenance.

Two-layer analysis:
  Layer 1 (VPC-wide): discover all resources co-located in the same VPC.
  Layer 2 (security-group chain): trace inbound/outbound SG rule references.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from services.aliyun_cloud_sdk import (
    describe_db_instances,
    describe_health_status,
    describe_instances,
    describe_load_balancers,
    describe_security_group_attribute,
    search_resource_center_resources,
)


def _find_vpc_id(instances: list[dict], instance_id: str) -> str:
    for inst in instances:
        if inst.get("InstanceId") == instance_id:
            vpc = inst.get("VpcAttributes") or {}
            return vpc.get("VpcId", "")
    return ""


def _instance_summary(inst: dict) -> dict:
    vpc = inst.get("VpcAttributes") or {}
    sgs = inst.get("SecurityGroupIds") or {}
    sg_list = sgs.get("SecurityGroupId", []) if isinstance(sgs, dict) else []
    memory_mb = int(inst.get("Memory") or 0)
    return {
        "instance_id": inst.get("InstanceId", ""),
        "name": inst.get("InstanceName", ""),
        "status": inst.get("Status", ""),
        "spec": inst.get("InstanceType", ""),
        "cpu": inst.get("Cpu", 0),
        "memory": memory_mb / 1024 if memory_mb else 0,
        "private_ip": vpc.get("PrivateIpAddress", {}).get("IpAddress", [""])[0] if isinstance(vpc.get("PrivateIpAddress"), dict) else "",
        "vswitch_id": vpc.get("VSwitchId", ""),
        "security_group_ids": sg_list,
    }


def analyze_impact(
    instance_id: str,
    region_id: str = "cn-beijing",
) -> dict[str, Any]:
    """Run two-layer impact analysis for a target ECS instance."""
    result: dict[str, Any] = {
        "target": {},
        "vpc_resources": {"ecs": [], "rds": [], "slb": [], "others": []},
        "sg_chain": [],
        "slb_backends": [],
    }

    # ── Resolve target instance ──────────────────────────
    instances = describe_instances(region_id=region_id, instance_ids=[instance_id])
    if not instances:
        result["error"] = f"未找到实例 {instance_id}"
        return result
    target = instances[0]
    result["target"] = _instance_summary(target)
    vpc_id = _find_vpc_id([target], instance_id)
    if not vpc_id:
        result["error"] = f"实例 {instance_id} 不在 VPC 内（经典网络）"
        return result
    result["vpc_id"] = vpc_id

    # ── Layer 1: VPC-wide discovery ──────────────────────
    all_ecs = describe_instances(region_id=region_id, vpc_id=vpc_id)
    result["vpc_resources"]["ecs"] = [_instance_summary(i) for i in all_ecs]

    try:
        all_rds = describe_db_instances(region_id=region_id, vpc_id=vpc_id)
        result["vpc_resources"]["rds"] = [
            {
                "instance_id": r.get("DBInstanceId", ""),
                "name": r.get("DBInstanceDescription", ""),
                "engine": f"{r.get('Engine', '')} {r.get('EngineVersion', '')}",
                "status": r.get("DBInstanceStatus", ""),
                "spec": r.get("DBInstanceClass", ""),
            }
            for r in all_rds
        ]
    except Exception:
        pass

    all_slb: list[dict[str, Any]] = []
    try:
        all_slb = describe_load_balancers(region_id=region_id, vpc_id=vpc_id)
        result["vpc_resources"]["slb"] = [
            {
                "load_balancer_id": lb.get("LoadBalancerId", ""),
                "name": lb.get("LoadBalancerName", ""),
                "address": lb.get("Address", ""),
                "status": lb.get("LoadBalancerStatus", ""),
            }
            for lb in all_slb
        ]
    except Exception:
        pass

    # Other resources via Resource Center
    try:
        rc = search_resource_center_resources(region_id=region_id)
        vpc_prefixes = {"ACS::ECS::", "ACS::RDS::", "ACS::SLB::", "ACS::NLB::", "ACS::ALB::"}
        others = [
            {
                "resource_id": r.get("ResourceId", ""),
                "resource_type": r.get("ResourceType", ""),
                "resource_name": r.get("ResourceName", ""),
                "region_id": r.get("RegionId", ""),
            }
            for r in rc
            if r.get("RegionId") == region_id and r.get("ResourceType", "") not in vpc_prefixes
        ]
        # Deduplicate by resource_id
        seen = set()
        unique_others = []
        for o in others:
            if o["resource_id"] not in seen:
                seen.add(o["resource_id"])
                unique_others.append(o)
        result["vpc_resources"]["others"] = unique_others[:20]
    except Exception:
        pass

    # ── Layer 2: Security group chain ────────────────────
    target_sg_ids = result["target"].get("security_group_ids", [])
    visited_sgs: set[str] = set()

    def _trace_sg(sg_id: str, depth: int = 0):
        if depth > 5 or sg_id in visited_sgs:
            return
        visited_sgs.add(sg_id)
        try:
            sg_info = describe_security_group_attribute(
                region_id=region_id, security_group_id=sg_id
            )
        except Exception:
            return
        permissions = sg_info.get("permissions", [])
        # Group rules by referenced security group
        refs: dict[str, list[dict]] = defaultdict(list)
        for perm in permissions:
            src_group = perm.get("SourceGroupId", "") or perm.get("DestGroupId", "")
            if src_group and src_group != sg_id:
                refs[src_group].append(perm)
        node = {
            "sg_id": sg_id,
            "sg_name": sg_info.get("security_group_name", ""),
            "rule_count": len(permissions),
            "referenced_groups": [
                {
                    "sg_id": ref_id,
                    "rules": [
                        f"{r.get('Direction','')} {r.get('IpProtocol','')}/{r.get('PortRange','')} ({r.get('Policy','')})"
                        for r in rules_list[:3]
                    ],
                    "total_rules": len(rules_list),
                }
                for ref_id, rules_list in refs.items()
            ],
        }
        result["sg_chain"].append(node)
        for ref_id in refs:
            _trace_sg(ref_id, depth + 1)

    for sg_id in target_sg_ids:
        _trace_sg(sg_id)

    # ── SLB backend check ────────────────────────────────
    target_ip = result["target"].get("private_ip", "")
    for slb in all_slb:
        lb_id = slb.get("LoadBalancerId", "")
        if not lb_id:
            continue
        try:
            backends = describe_health_status(region_id=region_id, load_balancer_id=lb_id)
            for be in backends:
                if be.get("ServerIp") == target_ip:
                    result["slb_backends"].append({
                        "load_balancer_id": lb_id,
                        "load_balancer_name": slb.get("LoadBalancerName", ""),
                        "port": be.get("Port", ""),
                        "health_status": be.get("ServerHealthStatus", ""),
                    })
        except Exception:
            pass

    return result


def render_impact_report(analysis: dict, maintenance_type: str = "") -> str:
    """Render analysis result as a conversation-flow markdown report."""
    if analysis.get("error"):
        return f"影响分析失败：{analysis['error']}"

    target = analysis.get("target", {})
    vpc_id = analysis.get("vpc_id", "")
    vpc = analysis.get("vpc_resources", {})

    lines = ["## 影响分析报告\n"]

    # ── Target ───────────────────────────────────────────
    lines.append("### 目标资源")
    lines.append(
        f"- 实例: **{target.get('name') or target.get('instance_id')}** "
        f"（{target.get('spec', '')} / {target.get('cpu')}C{target.get('memory')}G）"
    )
    lines.append(f"- 状态: {target.get('status', '')} | IP: {target.get('private_ip', '')}")
    lines.append(f"- VPC: {vpc_id}")
    sg_names = [g.get("sg_name", g.get("sg_id", "")) for g in analysis.get("sg_chain", [])]
    lines.append(f"- 安全组: {', '.join(sg_names) if sg_names else '无'}")
    if maintenance_type:
        lines.append(f"- 检修类型: {maintenance_type}")
    lines.append("")

    # ── VPC resources overview ───────────────────────────
    lines.append("### 同 VPC 资源总览")
    ecs_count = len(vpc.get("ecs", []))
    rds_count = len(vpc.get("rds", []))
    slb_count = len(vpc.get("slb", []))
    others_count = len(vpc.get("others", []))
    total = ecs_count + rds_count + slb_count + others_count
    parts = []
    if ecs_count:
        parts.append(f"ECS ×{ecs_count}")
    if rds_count:
        parts.append(f"RDS ×{rds_count}")
    if slb_count:
        parts.append(f"SLB ×{slb_count}")
    if others_count:
        parts.append(f"其他 ×{others_count}")
    lines.append(f"VPC {vpc_id} 内共 **{total}** 个资源（{' / '.join(parts)}）\n")

    # ECS details
    if ecs_count > 1:
        lines.append("**ECS 实例：**")
        for ecs in vpc.get("ecs", []):
            marker = " ← 当前" if ecs.get("instance_id") == target.get("instance_id") else ""
            lines.append(
                f"- {ecs.get('name') or ecs.get('instance_id')} "
                f"（{ecs.get('spec','')} / {ecs.get('status','')}）{marker}"
            )
        lines.append("")

    # RDS details
    if rds_count:
        lines.append("**RDS 实例：**")
        for rds in vpc.get("rds", []):
            lines.append(f"- {rds.get('name') or rds.get('instance_id')}（{rds.get('engine','')} / {rds.get('spec','')} / {rds.get('status','')}）")
        lines.append("")

    # SLB details
    if slb_count:
        lines.append("**SLB 实例：**")
        for slb in vpc.get("slb", []):
            lines.append(f"- {slb.get('name') or slb.get('load_balancer_id')}（{slb.get('address','')} / {slb.get('status','')}）")
        lines.append("")

    # ── Security group chain ─────────────────────────────
    sg_chain = analysis.get("sg_chain", [])
    if len(sg_chain) > 1:
        lines.append("### 安全组依赖链路")
        for node in sg_chain:
            marker = " ← 目标实例" if node["sg_id"] in target.get("security_group_ids", []) else ""
            lines.append(f"**{node.get('sg_name') or node['sg_id']}**{marker}（{node.get('rule_count', 0)} 条规则）")
            for ref in node.get("referenced_groups", []):
                lines.append(f"  └─ 引用 **{ref['sg_id']}**: {ref['total_rules']} 条规则")
                for rule in ref.get("rules", []):
                    lines.append(f"      {rule}")
        lines.append("")

    # ── SLB backends ─────────────────────────────────────
    slb_backends = analysis.get("slb_backends", [])
    if slb_backends:
        lines.append("### SLB 后端关联")
        lines.append(f"目标实例作为以下 SLB 的后端服务器：")
        for be in slb_backends:
            lines.append(
                f"- **{be.get('load_balancer_name') or be['load_balancer_id']}** "
                f"端口 {be.get('port','')} / 健康状态: {be.get('health_status','')}"
            )
        lines.append("")

    # ── Impact assessment ────────────────────────────────
    lines.append("### 影响评估")
    lines.append("| 资源 | 影响等级 | 说明 |")
    lines.append("|------|---------|------|")

    for ecs in vpc.get("ecs", []):
        eid = ecs.get("instance_id", "")
        if eid == target.get("instance_id"):
            continue
        lines.append(f"| {ecs.get('name') or eid} | 无 | 同 VPC 独立实例，不共享安全组 |")

    for rds in vpc.get("rds", []):
        target_sg_ids = target.get("security_group_ids", [])
        affected = any(
            ref.get("sg_id", "") == rds.get("instance_id", "")
            for node in sg_chain
            for ref in node.get("referenced_groups", [])
        )
        level = "中" if affected else "低"
        desc = "安全组规则依赖，连通性可能受影响" if affected else "独立资源，无直接依赖"
        lines.append(f"| {rds.get('name') or rds.get('instance_id')} | {level} | {desc} |")

    if slb_backends:
        for be in slb_backends:
            lines.append(f"| {be.get('load_balancer_name') or be['load_balancer_id']} | 中 | 目标实例是其后端服务器，变更期间需摘除 |")

    for slb in vpc.get("slb", []):
        lb_id = slb.get("load_balancer_id", "")
        if not any(be.get("load_balancer_id") == lb_id for be in analysis.get("slb_backends", [])):
            lines.append(f"| {slb.get('name') or lb_id} | 低 | 同 VPC 但无直接后端关联 |")

    for other in vpc.get("others", [])[:5]:
        lines.append(f"| {other.get('resource_name') or other['resource_id']} | 低 | 同地域资源，无 VPC 内直接依赖 |")

    return "\n".join(lines)
