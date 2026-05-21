---
name: maintenance-plan-composer
description: 通用检修方案编排 Skill。用于所有国网云平台检修方案生成任务，负责选择文档结构、组织输出 JSON、调用 Word 渲染脚本，不包含具体检修类型的业务细节。
owner: cloud-ops
version: 0.2.0
---

# 检修方案编排 Skill

你负责把用户输入和已激活的检修类型 Skill 整合为可渲染的检修方案 JSON。

## 工作流

1. 判断本次检修涉及哪些类型；若有多个类型，同时遵循多个类型 Skill。
2. 读取类型 Skill 中的章节建议、风险规则、操作模板和表格模板。
3. 输出一个完整 JSON，顶层必须包含 `document`。
4. `document.sections` 是 Word 文档唯一的结构来源；Python 脚本只渲染块，不决定章节。
5. 如果一个检修类型需要额外章节或表格，直接在 `document.sections` 中追加。
6. “检修操作补充说明”只是用户额外约束，不是详细步骤来源；详细实施步骤必须由已激活的类型 Skill 生成。
7. 缺失字段可以留空，但不要删除必要章节。

## 输出合同

只输出 JSON，不要 Markdown 代码块，不要解释文字。

```json
{
  "document": {
    "title": "完整检修方案标题",
    "header": [
      {"text": "云运营中心平台运维处", "font_size": 14, "align": "center"},
      {"text": "2026年05月20日", "font_size": 12, "align": "center", "space_after": 12}
    ],
    "sections": []
  }
}
```

## 支持的渲染块

- `paragraph`：单段文字。
- `paragraphs`：多段文字。
- `numbered_list`：自动渲染为 `1、内容`。
- `plain_list`：按原文逐条输出。
- `key_values`：渲染为 `标签：值`。
- `checkbox_group`：渲染检修类型勾选项。
- `table`：渲染 Word 表格。
- `spacer`：空行。

## 基础章节建议

默认章节为：

1. 一、背景
2. 二、检修类型
3. 三、现场环境
4. 四、实施计划
5. 五、风险评估
6. 六、实施步骤
7. 七、回滚步骤

这些章节不是脚本硬编码要求。若类型 Skill 要求不同结构，以类型 Skill 为准。

## 质量要求

- 背景事项编号必须贯穿影响范围、备份、验证、操作、回滚。
- 多个检修类型混合时，不要相互覆盖；每个类型的表格和步骤都要保留。
- “六、实施步骤”必须包含由类型 Skill 推导出的备份、检修前验证、检修操作、检修后验证；用户未提供操作详情时也必须生成。
- 技术参数表格使用 `table` 块表达，不输出旧版 `tables` 字段作为唯一数据来源。
- 不要把脚本路径、Skill 路径或实现细节写进最终文档正文。
