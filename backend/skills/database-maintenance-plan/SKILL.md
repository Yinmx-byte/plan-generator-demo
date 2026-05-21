---
name: database-maintenance-plan
description: 数据库类检修方案 Skill。用户提到 PolarDB、MongoDB、MySQL、数据库创建、白名单、数据库规格、存储空间、数据库版本时使用。
owner: cloud-ops
version: 0.1.0
---

# 数据库检修 Skill

## 适用场景

- 创建 PolarDB、MongoDB 或其他数据库实例。
- 修改数据库白名单、规格、存储、版本等参数。
- 数据库扩容或配置变更。

## 文档结构补充

实施步骤必须写清：

- 登录 ASCM 平台。
- 选择对应数据库产品。
- 选择组织和资源集。
- 填写数据库类型、版本、规格、存储、网络、白名单等参数。
- 提交前复核实例名称、网络和白名单。

如用户提供数据库参数，在文末追加对应数据库参数表，使用 `table` 块。

## PolarDB 参数表列

```json
[
  {"key": "instance_name", "label": "实例名称"},
  {"key": "spec", "label": "实例规格"},
  {"key": "storage", "label": "存储空间"},
  {"key": "chip_arch", "label": "芯片架构"},
  {"key": "db_type", "label": "数据库类型"},
  {"key": "db_version", "label": "数据库版本"},
  {"key": "vpc", "label": "VPC ID或名称"},
  {"key": "vswitch", "label": "Vswitch ID或名称"},
  {"key": "whitelist", "label": "白名单"},
  {"key": "count", "label": "数量"},
  {"key": "env", "label": "环境"}
]
```

## MongoDB 参数表列

```json
[
  {"key": "instance_name", "label": "实例名称"},
  {"key": "instance_id", "label": "实例id"},
  {"key": "current_spec", "label": "当前规格"},
  {"key": "target_spec", "label": "目标规格"},
  {"key": "env", "label": "环境"}
]
```

## 风险与回滚

- 创建数据库通常对业务无影响。
- 数据库配置变更需检查备份或快照。
- 白名单变更需重点复核访问源和业务连通性。
- 回滚为删除新建实例或恢复变更前配置。
