---
name: maintenance-plan-workflow
description: 检修方案生成系统的总控工作流 Skill。用于所有用户输入的首轮路由，判断普通聊天、新建方案、重新生成、修改已有方案、验证方案，并规定字段收集、Skill 选择、RAG 检索、DOCX 生成和可选 MCP 验证的端到端流程。
owner: cloud-ops
version: 0.1.0
---

# 检修方案总控工作流

本 Skill 是系统入口。所有用户消息先按本工作流判断意图，再决定是否进入检修方案生成链路。

## 意图分类

只允许以下四类意图：

1. `chat`
   - 普通交流、打招呼、询问系统能力、询问原理、询问如何配置/调试/使用项目。
   - 不生成 DOCX，不读取上一版文档，不检索方案模板。
   - 直接自然语言回答。

2. `generate`
   - 用户提供检修需求，或明确要求生成新的检修方案。
   - 需要抽取需求字段、检查字段完整性、选择产品 Skill、检索 RAG、生成 DOCX。

3. `regenerate`
   - 会话中已经有生成结果，用户要求“重新生成 / 再生成 / 重做 / 按原需求生成一遍”。
   - 不是修改上一版内容。
   - 默认复用当前已收集字段，不读取上一版 DOCX，不把“重新生成一遍”写入背景。
   - 如果用户同时贴了新的完整需求，则可重新抽取字段。

4. `edit`
   - 会话中已经有生成结果，用户要求修改上一版文档。
   - 例如：变更检修人员名单、替换某章节、按照某个文档重新评估风险点、补充实施步骤、调整回滚策略。
   - 需要读取上一版 DOCX 文本作为基线，保留未被点名修改的内容。

没有已生成文档时，禁止返回 `edit` 或 `regenerate`。这类消息应根据语义改判为 `generate` 或 `chat`。

## 路由输出

总控路由必须输出 JSON：

```json
{
  "intent": "chat|generate|regenerate|edit",
  "should_extract": true,
  "reason": "简短说明"
}
```

`should_extract` 规则：

- `chat`: 必须为 `false`。
- `generate`: 通常为 `true`。
- `regenerate`: 如果只是“重新生成一遍”等短指令，为 `false`；如果附带新的完整需求，为 `true`。
- `edit`: 通常为 `true`，用于抽取人员、时间、约束等变更字段。

## 生成/修改工作链路

当 intent 为 `generate`、`regenerate` 或 `edit` 时，按顺序执行：

1. 抽取用户需求字段。
2. 检查必填字段是否完整。
3. 缺字段时继续追问，不生成文档。
4. 字段完整后，判断检修产品和动作类型。
5. 选择并读取对应产品 Skill：
   - ECS：`ecs-lifecycle-maintenance`
   - k8s：`k8s-worker-maintenance`
   - MQ：`mq-maintenance-plan`
   - OSS：`oss-maintenance-plan`
   - PolarDB：`polardb-maintenance-plan`
   - RDS/DRDS/MySQL：`rds-maintenance-plan`
   - Redis：`redis-maintenance-plan`
   - SLB：`slb-maintenance-plan`
6. 始终使用 `maintenance-plan-composer` 约束文档结构和输出 JSON。
7. 检索 RAG，优先寻找同产品、同动作、同系统或同字段结构的历史方案。
8. 生成完整 `document` JSON。
9. 调用 DOCX 渲染工具生成 Word 文档。
10. 如果前端勾选验证，则调用 Page Agent / MCP 执行只读验证。

## 修改已有文档

当 intent 为 `edit`：

1. 读取上一版 DOCX 文本。
2. 使用 `docx-document-editor` Skill。
3. 用户没有点名的章节必须尽量保持原样。
4. 输出完整修订版 `document` JSON，而不是只输出变更片段。

## 普通聊天

当 intent 为 `chat`：

1. 不进入字段抽取。
2. 不进入 Skill/RAG 文档生成链路。
3. 可以解释系统能力、需要哪些字段、当前已收集了哪些信息、如何触发生成或验证。
4. 回答要简洁准确。

## 验证边界

Page Agent / MCP 验证只能做只读浏览器验证和方案检查，不得执行真实生产变更、删除、重启、扩缩容、创建资源等不可逆操作。

