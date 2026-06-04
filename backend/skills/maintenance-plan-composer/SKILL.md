---
name: maintenance-plan-composer
description: 通用检修方案编排 Skill。所有国网云平台检修方案生成任务都必须使用它统一首页、章节骨架、风险评估、实施步骤和 JSON 输出结构；具体云产品操作细节再叠加对应产品 Skill。
owner: cloud-ops
version: 0.4.0
---

# 检修方案编排 Skill

你负责把用户输入、RAG 历史方案片段、产品检修 Skill 整合为可渲染的完整检修方案 JSON。

## 强制原则

1. 不要自由编造章节标题；默认必须使用下方固定骨架。
2. 不要写宽泛空话，尤其是风险评估和实施步骤。
3. 用户未提供细节时，必须由产品 Skill 推导可执行步骤；缺少具体实例参数时用“待实施前确认”占位，不得省略步骤。
4. `document.sections` 是 Word 文档唯一结构来源，必须输出完整章节，不输出片段。
5. 最终只输出 JSON 对象，不要 Markdown，不要解释。
6. 最终输出前调用 `build_maintenance_document` 工具，用完整 JSON 做渲染检查；工具通过后再输出同一份完整 JSON。

## 首页格式

`document` 必须包含：

```json
{
  "title": "{网络环境}{系统/组件}{动作}检修方案",
  "cover": {
    "logo_width_cm": 3.1,
    "top_spacers": 7,
    "middle_spacers": 8,
    "title_font_size": 22,
    "title_font_name": "方正小标宋_GBK"
  },
  "header": [
    {"text": "云运营中心平台运维处", "font_size": 16, "font_name": "仿宋_GB2312", "align": "center"},
    {"text": "YYYY年M月D日", "font_size": 16, "font_name": "仿宋_GB2312", "align": "center"}
  ],
  "sections": []
}
```

渲染器会自动在第一页左上角放国网 logo，中间放标题，下方放部门处室和日期，并在正文前分页。

标题必须是短标题，格式为：

`{网络环境}{系统/组件/资源对象}{动作}检修方案`

示例：

- `内网总部ESB组件创建ECS实例检修方案`
- `外网网上国网灰度创建ECS实例检修方案`
- `内网人力资源2.0系统创建ECS实例检修方案`

禁止把用户需求整句、背景说明、问题工单说明写入标题。例如不要输出：

`内网总部ESB组件因业务扩容需要新增ECS云服务器实例，项目组已提报问题工单，需通过本次检修完成资源创建和配置确认。检修方案`

## 固定章节骨架

正文必须按这个顺序输出。渲染器会按顺序显示为 `一、背景`、`二、检修类型`、`三、现场环境`、`四、实施计划`、`五、风险评估`、`六、实施步骤`、`七、回滚步骤`；Skill 输出中一级标题可以只写标题名，不要自行打乱顺序：

1. `背景`
2. `检修类型`
3. `现场环境`
4. `实施计划`
5. `风险评估`
6. `实施步骤`
7. `回滚步骤`

### 背景

写具体事项，不要泛化。例如：

- `{系统名称}{动作}`
- `该事项项目组提报问题工单，需检修进行处理，实现问题工单闭环。`

### 检修类型

用 `checkbox_group`，固定六项：

- 配置变更
- 组件升级
- 组件扩缩容
- 数据库变更
- 日常维护（原硬件设备）
- 其他

### 现场环境

必须包含：

- `（1）内网环境/外网环境：{内网/外网/内、外网环境}`
- `（2）实施地点：国网亦庄数据中心二期运维专区`
- `（3）专有云版本：v3.16`
- `（4）涉及的组件实例信息：`
- 每个事项依次写：`1、{事项名}`、`组织：{组织}`、`资源集：{资源集}`
- 若产品 Skill 要求资源参数表，放在本章后面，格式用 `table`。

### 实施计划

必须包含：

- `4.1 检修窗口`，下接表格，列为 `年份 / 开始时间 / 结束时间`
- `4.2 实施人员`，下接表格，列为 `方案提供人 / 检修执行人 / 检修复核人 / 业务系统参与人 / 安全责任人`

### 风险评估

必须严格包含：

- `5.1影响范围`
- `5.2危险点分析`
- `5.3安全措施`
- `5.3.1授权`
- `5.3.2备份`
- `5.3.3验证`
- `5.3.4 双人复核`

危险点分析默认四项，产品 Skill 可追加但不得替换这四项：

1. `授权不当危险点：授权过大，导致操作影响预定方案以外的生产环境实例。`
2. `备份不当危险点：检修前实例未进行备份或备份无效，导致操作失败后无法完成应急回退。`
3. `验证不当危险点：{产品对象}操作对象，以及服务是否存在单点隐患未核实清楚，导致操作后出现业务影响。`
4. `双人复核不当危险点：双人复核不仔细，导致操作错误执行而出现业务影响。`

安全措施必须具体：

- 授权：写 ASCM 授权范围、授权账号、堡垒机账号。
- 备份：写本次是否涉及备份；创建类通常“不涉及备份”，升配/降配/重启/回收类按产品 Skill 写快照、配置备份或业务确认。
- 验证：写检修前必须检查的对象，例如资源集 IP 是否充足、实例状态、服务单点隐患、现有业务状态。
- 双人复核：固定包含“确认在正确的组织和资源集下做操作，检查实例操作对象是否正确；严格按照文档复核关键步骤及关键点。”

### 实施步骤

必须严格包含：

- `6.1备份`
- `6.2 检修前验证`
- `6.3 检修操作`
- `6.3.1 {事项名}`
- `6.4 检修后验证`

`6.3 检修操作` 必须能指导后续验证。不能只写“按 Skill 执行”“核对参数”。必须写到控制台路径、选择对象、填写字段、提交、确认成功等动作。

### 回滚步骤

必须包含：

- `7.1 回滚操作`
- `7.2 回滚后验证`

回滚步骤要对应本次动作。创建实例类回滚通常是删除新建实例；升降配类回滚是恢复原规格；回收类回滚是按备份/原配置重建或恢复。

## 输出块建议

- 一级标题用 section `heading`。
- `4.1/5.1/6.1/7.1` 这类小节用 `{ "type": "heading", "level": 2, "text": "..." }`。
- 操作句子用 `paragraphs` 或 `numbered_list`，不要把多个关键动作压成一句。
- 参数清单必须优先用表格表达。

## RAG 使用边界

- Skill 决定结构、必填小节和操作最低颗粒度。
- RAG 只用于补充历史方案原文、真实系统名、组织、资源集、账号、产品参数、历史操作措辞。
- Skill 与 RAG 冲突时，以 Skill 的硬性规则为准。

## Word 渲染 JSON 格式契约

生成方案时必须直接输出可被 `build_document` 渲染的 JSON。格式是方案的一部分，不允许自由发挥。

### 顶层结构

最终 JSON 必须包含：

```json
{
  "title": "内网总部ESB组件创建ECS实例检修方案",
  "department": "云运营中心平台运维处",
  "date": "2026年05月31日",
  "document": {
    "title": "内网总部ESB组件创建ECS实例检修方案",
    "cover": {
      "logo_width_cm": 3.1,
      "top_spacers": 7,
      "middle_spacers": 8
    },
    "header": [
      {"text": "云运营中心平台运维处", "font_size": 14, "align": "center"},
      {"text": "2026年05月31日", "font_size": 12, "align": "center"}
    ],
    "sections": []
  }
}
```

### Section 规则

`document.sections` 中每个章节必须使用：

```json
{
  "heading": "背景",
  "level": 1,
  "blocks": []
}
```

禁止只写：

```json
{"heading": "背景", "content": "..."}
```

禁止只输出标题没有 `blocks`。所有正文、表格、复选框、步骤都必须放进 `blocks`。

### Block 类型

只能使用下列 block 类型。

#### 标题块

用于 `4.1`、`5.1`、`6.1` 等小节：

```json
{"type": "heading", "level": 2, "text": "6.1备份"}
```

#### 普通段落

```json
{"type": "paragraph", "text": "内网总部ESB组件创建ECS实例不涉及备份。", "first_line_indent": 0.74}
```

#### 多段正文

```json
{
  "type": "paragraphs",
  "items": [
    "授权不当危险点：授权过大，导致操作影响预定方案以外的生产环境实例。",
    "验证不当危险点：ECS操作对象、组织、资源集、VPC、VSwitch、安全组、镜像、规格、IP地址余量未核实清楚，导致创建失败或业务不可达。"
  ]
}
```

#### 编号步骤

用于实施步骤和回滚步骤：

```json
{
  "type": "numbered_list",
  "items": [
    "使用ascm_demo账号，登录内网ASCM平台，产品选择“云服务器ECS”。",
    "选择组织“数字化工作部”-资源集“总部ESB组件系统资源集”，进入云服务器ECS实例列表。"
  ]
}
```

#### 复选框组

用于检修类型：

```json
{
  "type": "checkbox_group",
  "per_line": 2,
  "items": [
    {"label": "配置变更", "checked": true},
    {"label": "组件升级", "checked": false},
    {"label": "组件扩缩容", "checked": false},
    {"label": "数据库变更", "checked": false},
    {"label": "日常维护（原硬件设备）", "checked": false},
    {"label": "其他", "checked": false, "extra": ""}
  ]
}
```

#### 表格

所有实施计划、人员、产品参数表必须用 `table`，不得写成纯文本。

```json
{
  "type": "table",
  "columns": [
    {"key": "year", "label": "年份"},
    {"key": "start_time", "label": "开始时间"},
    {"key": "end_time", "label": "结束时间"}
  ],
  "rows": [
    {"year": "2026年", "start_time": "06月01日 10:00", "end_time": "06月01日 12:00"}
  ]
}
```

### 固定章节格式模板

每份方案必须至少包含如下结构。产品 Skill 可以补充表格列和具体步骤，但不能移除这些章节。

```json
{
  "document": {
    "sections": [
      {
        "heading": "背景",
        "level": 1,
        "blocks": [
          {"type": "paragraph", "text": "{具体事项背景}", "first_line_indent": 0.74},
          {"type": "paragraph", "text": "以上事项项目组提报问题工单，需检修进行处理，实现问题工单闭环。", "first_line_indent": 0.74}
        ]
      },
      {
        "heading": "检修类型",
        "level": 1,
        "blocks": [
          {"type": "checkbox_group", "per_line": 2, "items": []}
        ]
      },
      {
        "heading": "现场环境",
        "level": 1,
        "blocks": [
          {"type": "key_values", "items": [
            {"label": "（1）内网环境/外网环境", "value": "{network}"},
            {"label": "（2）实施地点", "value": "{location}"},
            {"label": "（3）专有云版本", "value": "v3.16"}
          ]},
          {"type": "paragraph", "text": "（4）涉及的组件实例信息："},
          {"type": "table", "columns": [], "rows": []}
        ]
      },
      {
        "heading": "实施计划",
        "level": 1,
        "blocks": [
          {"type": "heading", "level": 2, "text": "4.1 检修窗口"},
          {"type": "table", "columns": [], "rows": []},
          {"type": "heading", "level": 2, "text": "4.2 实施人员"},
          {"type": "table", "columns": [], "rows": []}
        ]
      },
      {
        "heading": "风险评估",
        "level": 1,
        "blocks": [
          {"type": "heading", "level": 2, "text": "5.1影响范围"},
          {"type": "paragraphs", "items": []},
          {"type": "heading", "level": 2, "text": "5.2危险点分析"},
          {"type": "paragraphs", "items": []},
          {"type": "heading", "level": 2, "text": "5.3安全措施"},
          {"type": "heading", "level": 3, "text": "5.3.1授权"},
          {"type": "paragraphs", "items": []},
          {"type": "heading", "level": 3, "text": "5.3.2备份"},
          {"type": "paragraphs", "items": []},
          {"type": "heading", "level": 3, "text": "5.3.3验证"},
          {"type": "paragraphs", "items": []},
          {"type": "heading", "level": 3, "text": "5.3.4 双人复核"},
          {"type": "paragraphs", "items": []}
        ]
      },
      {
        "heading": "实施步骤",
        "level": 1,
        "blocks": [
          {"type": "heading", "level": 2, "text": "6.1备份"},
          {"type": "numbered_list", "items": []},
          {"type": "heading", "level": 2, "text": "6.2 检修前验证"},
          {"type": "numbered_list", "items": []},
          {"type": "heading", "level": 2, "text": "6.3 检修操作"},
          {"type": "heading", "level": 3, "text": "6.3.1 {事项名}"},
          {"type": "numbered_list", "items": []},
          {"type": "heading", "level": 2, "text": "6.4 检修后验证"},
          {"type": "numbered_list", "items": []}
        ]
      },
      {
        "heading": "回滚步骤",
        "level": 1,
        "blocks": [
          {"type": "heading", "level": 2, "text": "7.1 回滚操作"},
          {"type": "numbered_list", "items": []},
          {"type": "heading", "level": 2, "text": "7.2 回滚后验证"},
          {"type": "numbered_list", "items": []}
        ]
      }
    ]
  }
}
```

格式验收标准：

- `document.sections` 至少 7 个章节。
- 全文至少 8 个带 `type` 的 block。
- 至少包含 1 个 `table`，实际方案通常至少包含检修窗口表、实施人员表、产品参数表。
- 禁止把表格写成纯段落。
- 禁止把实施步骤压缩成一两句概述。
