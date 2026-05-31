---
name: mq-maintenance-plan
description: MQ 检修 Skill。用户提到 MQ、RocketMQ、GroupID/GID、Topic、队列、SSD 盘、MQ 实例创建/回收/配置调整时使用。
owner: cloud-ops
version: 0.1.0
---

# MQ 检修

## 资源表

- GID/Topic 回收：实例 ID、GroupID、Topic、所属环境、业务确认人。
- SSD/磁盘配置：IP 地址、SSD 大小、部署位置、环境。
- 配置调整：实例 ID、当前配置、目标配置、环境。

## 实施步骤

- 检修前确认 MQ 实例、Topic/GID 归属、消费组状态和业务确认结果。
- 回收无用 GID/Topic 前检查近期消费、堆积、连接和项目组确认。
- 创建 SSD 或调整配置前确认节点位置、磁盘容量和实例状态。
- 操作后检查控制台状态、消息堆积、生产/消费链路。

## 风险与回滚

- MQ 误删 GID/Topic 可能影响消息消费，必须先确认无业务使用。
- 配置调整异常时恢复原配置。
- 回收类操作如无法直接恢复，应写明按原配置重新创建并联系项目组验证。
