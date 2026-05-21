---
name: ecs-instance-provisioning
description: ECS 云服务器实例创建/配置类检修方案 Skill。用户提到 ECS、云服务器、创建实例、实例规格、镜像、VPC、VSwitch、安全组时使用。
owner: cloud-ops
version: 0.1.0
---

# ECS 实例检修 Skill

## 适用场景

- 创建 ECS 实例。
- 调整 ECS 相关配置。
- 为业务系统补充云服务器资源。

## 文档结构补充

在实施步骤中必须包含：

- 登录 ASCM 平台。
- 产品选择“云服务器 ECS”。
- 选择对应组织和资源集。
- 进入 ECS 实例并执行创建或配置操作。
- 填写实例规格、镜像、磁盘、VPC、VSwitch、安全组、数量等参数。
- 提交前双人复核。

如用户提供 ECS 参数，在文末追加 `ECS实例参数表`，使用 `table` 块。

## ECS 参数表列

```json
[
  {"key": "cloud_env", "label": "云环境"},
  {"key": "instance_name", "label": "实例名称"},
  {"key": "disk", "label": "磁盘"},
  {"key": "image", "label": "自定义镜像"},
  {"key": "password", "label": "密码"},
  {"key": "vpc", "label": "VPC ID或名称"},
  {"key": "vswitch", "label": "Vswitch ID或名称"},
  {"key": "security_group", "label": "安全组"},
  {"key": "spec", "label": "实例规格"},
  {"key": "count", "label": "数量"}
]
```

## 风险与回滚

- 创建 ECS 通常对业务无影响。
- 备份通常写“不涉及备份”。
- 检修前验证资源集 IP、配额、规格、镜像、安全组是否满足要求。
- 回滚通常为删除新建 ECS 或恢复原配置。
