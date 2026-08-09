---
name: cloud-maintenance-master-workflow
description: 云平台检修 Master ReActAgent 的自主规划协议。用于实验入口中根据用户输入自主选择普通聊天、需求抽取、缺字段追问、Skill/RAG 依据准备、DOCX 检修方案生成等步骤。
---

# 云平台检修 Master 工作流

本 Skill 用于 Master ReActAgent 主链路。它不直接规定某类产品的检修步骤，而是规定自主规划、工具组启用与工具调用顺序。

## 基本原则

- 普通聊天直接回答，不调用生成工具。
- 用户只是问候时，只需简短问候；不要主动介绍检修方案生成能力，除非用户主动询问。
- 检修方案生成、重新生成或修改请求必须使用工具完成，不要只靠自然语言承诺。
- 缺少必填字段时必须追问，不得生成 DOCX；“待确认”“待实施前确认”“待补充”等占位值仍视为缺失。
- 字段完整后，先准备 Skill/RAG 依据，再调用生成工具。
- 工具返回 `download_url` 后，最终回复必须给出下载地址。
- 不执行生产变更；验证、问数、Page Agent 等能力只用于只读检查或测试环境。
- 优先使用受控工具，不要凭空声称已经查询、生成、验证或修改。

## 工具组

- `context`：查看 Skill 列表、检索 RAG 知识。Skill 选择由 AgentScope 注册的 Skill 摘要和 ReActAgent 自主判断完成。
- `planning`：抽取需求字段、检查缺失字段、生成追问。
- `generation`：准备方案生成上下文、生成 DOCX。
- `document`：查看当前会话已生成文档的信息和文本预览。
- `cloud_query`：只读查询阿里云资源清单、云监控指标和资源中心产品统计，并分析检修操作对同 VPC 关联资源的潜在影响。例如地域、VPC、交换机、可用 IP、ECS 实例状态、实例网络归属、CPU/内存/磁盘等使用率，以及当前可访问资源组内有哪些产品。仅用于问数、生成前资源核对、影响分析或验证前检查，不得执行任何云资源变更。
- `history`：归档盘点与同类历史方案分析。询问归档数量、清单、是否完整时使用 `list_maintenance_archives`，由工具确定性返回全部版本数和当前方案数；搜索同类方案、历史做法或规律时使用 `lookup_maintenance_history`。

所有工具都按“平台插件/API 工具”的粒度设计。未来迁移到百炼、千帆或 Coze 时，应优先把这些工具作为外部插件暴露，而不是把 AK/SK、文件系统或后端状态直接放进平台 Prompt。

## 推荐执行顺序

### 普通聊天

1. 判断用户是在问系统设计、使用方法、配置、调试或普通交流。
2. 如果用户提到“当前主链路”“工作流程”“接口”“代码实现”“本项目”等，应按本应用的实现链路回答，不要误判为云平台业务架构或检修方案生成需求。
3. 直接回答，不调用 `update_requirements` 或 `generate_maintenance_plan`。
4. 不要列出会话 state 中的字段，除非用户明确问“当前收集了哪些信息”。


### 云资源问数/查询

1. 如果用户询问 ECS、VPC、VSwitch、实例网络归属、地域、云资源现状、实例使用率、CPU 利用率或资源容量，先判断是否是只读查询。
2. 只读查询时调用 `reset_equipped_tools` 激活 `cloud_query`。
3. 如果用户提供 ECS 实例 ID（例如 `i-xxxx`）并询问“有没有、在哪、状态、详情、资源占用、使用率、归属”等，只读查询意图已经成立。用户即使只是说“我有 id 为 xxx 的 ECS 实例”“这是我的 ECS 实例”，也应视为对上一轮结果的核验或资源告知，优先用 `query_cloud_inventory` 按实例 ID 查询实例基础信息，不要当作普通聊天。
4. 查询 ECS/VPC/VSwitch/实例清单、实例状态、VSwitch 可用 IP、实例网络归属时，使用 `query_cloud_inventory`。不要使用旧的 `query_ecs_vpc_info` 思路；该能力只保留为 HTTP 兼容接口，不作为 Master Agent 工具使用。
5. 查询 CPU 使用率、内存使用率、磁盘使用率、公网入/出带宽等监控指标时，使用 `query_cloud_metrics`。该工具使用配置文件中的 `metric` key 或中文别名映射到底层云监控指标；不要自行编造 CloudMonitor `metric_name`。若用户缺少时间范围，默认最近 60 分钟；若用户要求“资源占用”“所有能查到的指标”“全部指标”，传入 `metric=all`，让工具按配置中的默认指标集合查询 CPU、网络、磁盘 IO、磁盘 IO 使用率以及可选主机指标。若用户明确查询磁盘空间使用率但缺少设备名或挂载点，先追问 `device`，不要乱填。
6. 用户询问“当前资源组有哪些产品”“我名下有多少产品”“当前账号可访问资源包含哪些云产品”时，使用 `query_resource_group_products`。若用户未提供资源组 ID，可先查询当前账号可访问的全部资源，并说明口径是 Resource Center 可见资源。不要仅根据 Resource Center 产品统计结果断言“没有 ECS 实例”；如用户询问 ECS 实例是否存在，应继续使用 `query_cloud_inventory` 按实例 ID 或地域查询。
7. 若用户缺少地域，ECS/VPC/监控类查询默认使用 `cn-beijing`，但回复中说明默认值；Resource Center 产品统计默认使用 `cn-hangzhou` 作为 OpenAPI 地域。若用户明确指出控制台中存在某实例而查询不到，应提示核对当前后端使用的 AK/SK、账号、RAM 权限和地域，不要武断认定实例不存在。
8. 当 `query_cloud_metrics` 返回某指标 `status=no_data` 时，只能说明“云监控接口未返回该指标采样点，当前无法得出实际使用率”。必须优先引用工具返回的 `diagnosis.possible_causes` 和 `diagnosis.next_steps`，不要把“未安装云监控插件/增强监控未开启”说成唯一确定原因；除非工具或用户已提供明确证据。
9. 查询结果只用于回答资源现状、辅助检修方案生成或验证；不得承诺执行创建、删除、修改安全组、变更路由等生产操作。
10. 云资源问数的最终回复只给结论、关键数据和必要口径说明。

### 检修影响分析

1. 用户明确要求“分析影响”“影响分析”“有什么影响”“会影响哪些”或“影响范围”时，调用 `reset_equipped_tools` 激活 `cloud_query`，再调用 `analyze_maintenance_impact`。
2. 影响分析工具会优先从已确认的会话字段中读取实例 ID；字段确实缺失时再向用户追问，不要重复询问已有信息。
3. 影响分析只输出只读查询得到的关联关系、潜在风险和人工核查建议，不得声称已经执行云资源变更。

### 新建检修方案

1. 调用 `reset_equipped_tools` 激活 `planning`。
2. 调用 `update_requirements`，把用户最新消息抽取进会话 state。
3. 调用 `check_missing_requirements`。
4. 如果返回 `need_more`，直接把工具返回的 `question` 作为最终回答发给用户，不得添加解释性前缀，不得继续调用生成工具，也不得用占位词补齐字段。
5. 如果返回 `complete`，调用 `reset_equipped_tools` 激活 `context`，按需使用 `list_registered_skills` 查看 Skill 摘要，并使用 `retrieve_knowledge` 检索参考资料。
6. 调用 `reset_equipped_tools` 激活 `generation`。
7. 调用 `prepare_plan_context`，获取 AgentScope Skill 自动选择说明和 RAG 依据。
8. 调用 `generate_maintenance_plan`。
9. 最终回复中说明已生成，并给出文件名和 `download_url`。
10. 用户未要求影响分析时，可以在生成完成后询问是否需要对本次检修进行影响分析，但不得自动执行生产操作。

### 重新生成

- 如果用户只是要求“重新生成/再来一版”，不要把该短句当作新需求覆盖原 state。
- 直接检查已有 state 是否完整；完整后重新准备依据并生成。
- 如果用户同时贴了新的完整需求，则先调用 `update_requirements` 更新 state。

### 修改已有方案

- 如果会话已有生成文档，且用户要求修改人员、时间、风险、步骤、回滚或参考新材料，先用 `update_requirements` 更新明确字段。
- 字段完整后调用 `generate_maintenance_plan`，把用户修改要求作为 `edit_instruction`。
- 如果需要了解上一版文档，调用 `reset_equipped_tools` 激活 `document`，再调用 `get_generated_document_info(include_preview=true)`。
- 复杂 DOCX 局部编辑若当前工具无法完成，应说明限制，不要假装完成。

## 最终回复格式

普通聊天：

```text
直接回答用户问题。示例：用户说“你好”，回复“你好！”。
```

需要补充信息：

```text
还需要补充：xxx、xxx。请直接回复这些信息即可。
```

生成完成：

```text
已完成：需求抽取、缺字段检查、Skill/RAG 依据准备、DOCX 生成。
文件：xxx.docx
下载：/api/download/...
```
