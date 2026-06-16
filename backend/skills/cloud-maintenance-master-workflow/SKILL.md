---
name: cloud-maintenance-master-workflow
description: 云平台检修 Master ReActAgent 的自主规划协议。用于实验入口中根据用户输入自主选择普通聊天、需求抽取、缺字段追问、Skill/RAG 依据准备、DOCX 检修方案生成等步骤。
---

# 云平台检修 Master 工作流

本 Skill 用于 Master ReActAgent 主链路。它不直接规定某类产品的检修步骤，而是规定自主规划、工具组启用与工具调用顺序。

## 基本原则

- 普通聊天直接回答，不调用生成工具。
- 检修方案生成、重新生成或修改请求必须使用工具完成，不要只靠自然语言承诺。
- 缺少必填字段时必须追问，不得生成 DOCX。
- 字段完整后，先准备 Skill/RAG 依据，再调用生成工具。
- 工具返回 `download_url` 后，最终回复必须给出下载地址。
- 不执行生产变更；验证、问数、Page Agent 等能力只用于只读检查或测试环境。
- 优先使用受控工具，不要凭空声称已经查询、生成、验证或修改。

## 工具组

- `context`：查看 Skill 列表、按需求初筛 Skill、检索 RAG 知识。
- `planning`：抽取需求字段、检查缺失字段、生成追问。
- `generation`：准备方案生成上下文、生成 DOCX。
- `document`：查看当前会话已生成文档的信息和文本预览。

所有工具都按“平台插件/API 工具”的粒度设计。未来迁移到百炼、千帆或 Coze 时，应优先把这些工具作为外部插件暴露，而不是把 AK/SK、文件系统或后端状态直接放进平台 Prompt。

## 推荐执行顺序

### 普通聊天

1. 判断用户是在问系统设计、使用方法、配置、调试或普通交流。
2. 如果用户提到“当前主链路”“工作流程”“接口”“代码实现”“本项目”等，应按本应用的实现链路回答，不要误判为云平台业务架构或检修方案生成需求。
3. 直接回答，不调用 `update_requirements` 或 `generate_maintenance_plan`。

### 新建检修方案

1. 调用 `reset_equipped_tools` 激活 `planning`。
2. 调用 `update_requirements`，把用户最新消息抽取进会话 state。
3. 调用 `check_missing_requirements`。
4. 如果返回 `need_more`，直接把 `question` 发给用户。
5. 如果返回 `complete`，调用 `reset_equipped_tools` 激活 `context`，按需使用 `select_maintenance_skills` 和 `retrieve_knowledge`。
6. 调用 `reset_equipped_tools` 激活 `generation`。
7. 调用 `prepare_plan_context`，获取候选 Skill 和 RAG 依据。
8. 调用 `generate_maintenance_plan`。
9. 最终回复中说明已生成，并给出文件名和 `download_url`。

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
直接回答用户问题。
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
