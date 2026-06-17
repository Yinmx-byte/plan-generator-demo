# 国网云平台检修方案生成器

基于 AgentScope 的检修方案生成工作台。当前版本以对话入口为主：用户输入需求描述或需求文档后，系统先识别意图，再按需抽取关键信息、检索百炼知识库、读取对应 Skill，并生成标准化 Word 检修方案。

## 项目结构

```text
plan-generator-demo/
├── backend/
│   ├── main.py                    # FastAPI 服务入口
│   ├── agents/                    # Master Agent 与方案生成 Agent
│   ├── api/admin_routes.py        # Skill、百炼知识库等管理接口
│   ├── rag/                       # 百炼知识库 Retrieve 与管理封装
│   ├── scripts/generate_plan.py   # 配置驱动的 Word 文档渲染工具
│   ├── services/                  # 需求抽取、方案生成、Page Agent 等服务
│   ├── skills/                    # AgentScope Skill 与通用文档格式契约
│   ├── skills_runtime/            # Skill 元数据加载与路由
│   ├── mcp_servers.json           # 本地 MCP 接入配置
│   └── .env.example
├── frontend/
│   ├── index.html
│   ├── pages/                     # 方案生成、Skill、知识库、Page Agent 页面
│   ├── js/app.js
│   └── styles/app.css
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

常用配置：

```env
MODEL_PROVIDER=deepseek
MASTER_MODEL_NAME=deepseek-v4-flash
EXTRACTION_MODEL_NAME=deepseek-v4-flash
PLAN_MODEL_NAME=deepseek-v4-pro
PLAN_RETRY_MODEL_NAME=deepseek-v4-pro
PLAN_RETRY_THINKING_ENABLED=true
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com

ALIBABA_CLOUD_ACCESS_KEY_ID=xxx
ALIBABA_CLOUD_ACCESS_KEY_SECRET=xxx
BAILIAN_WORKSPACE_ID=ws-xxx
BAILIAN_INDEX_ID=xxx
```

### 3. 启动服务

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000`。

## 当前工作流

```text
用户输入
  └─ Master ReActAgent
      ├─ 普通聊天：直接回复
      ├─ 生成方案：调用 planning 工具抽取关键信息，缺失则追问
      ├─ 重新生成：复用已收集需求，重新准备 Skill/RAG 依据
      └─ 修改方案：读取会话状态和已生成文档信息，按用户要求修订
          ├─ context 工具：查看 AgentScope Skill 摘要，检索百炼 RAG
          ├─ generation 工具：调用方案生成服务
          │   ├─ compose_plan_json：由专用 Plan ReActAgent 读取 Skill 并输出结构 JSON
          │   ├─ validate_plan_json：检查章节、风险和实施步骤完整性
          │   └─ render_docx：只负责将已验证 JSON 渲染为 .docx
          └─ 返回下载地址
```

## 核心模块

- **Skill**：每类检修方案的稳定规则、章节结构、风险项、实施步骤约束和工具调用原则放在 `backend/skills/`。
- **百炼 RAG**：历史检修方案、通用模板和阿里云参考资料通过百炼知识库管理，业务生成链路通过 Retrieve API 获取切片。
- **MCP / Page Agent**：`backend/mcp_servers.json` 配置外部 MCP，当前用于 Page Agent 浏览器侧执行与验证。
- **方案生成服务**：`services/plan_generation.py` 将写文档能力封装为 `compose_plan_json`、`validate_plan_json`、`render_docx` 三步；其中 `compose_plan_json` 内部仍使用专用 Plan ReActAgent，方便加载 Skill 和调用渲染检查工具。
- **Word 渲染**：`scripts/generate_plan.py` 将已验证的内容 JSON 与通用格式契约合成为 `.docx`；默认格式位于 `skills/maintenance-plan-composer/references/document-style.json`，所有检修类型共用，渲染工具也支持显式传入 `style_contract`。
- **远程知识库管理**：前端知识库页可上传/查看/删除百炼文档；上传或删除后会自动提交重建索引任务。

## Master Agent 工具组配置

Master Agent 的工具实现位于 `backend/tools/`，由 `backend/tools/master_toolkit.py` 统一注册。默认注册 `context`、`planning`、`generation`、`document` 四个工具组；如需临时收窄工具面，可设置：

```env
MASTER_AGENT_TOOL_GROUPS=context,planning,generation,document
```

设置为 `all` 或不配置时启用全部工具组，设置为 `none`、`off` 或 `disabled` 时不注册分组工具。工具执行逻辑保留在 Python 函数中，稳定规则和业务约束继续放在 Skill 中。

## 方案生成稳定性配置

方案生成会输出完整 Word 渲染 JSON，建议保持较高输出上限：

```env
MAX_TOKENS=16000
```

默认不再返回泛化兜底方案：当模型 JSON 解析失败或结构不符合渲染契约时，后端会重试一次；重试仍失败则返回错误并保存 debug 输出，避免下载到内容不可用的 DOCX。如需临时恢复旧兜底行为，可显式设置：

```env
ALLOW_FALLBACK_PLAN=true
```

## API

### 对话与方案生成

- `POST /api/chat/stream`：主对话入口，基于 Master ReActAgent 自主规划，SSE 流式返回状态、工具调用 trace、追问和生成结果。
- `POST /api/chat`：非流式 Master ReActAgent 对话入口。
- `POST /api/agent/stream`：`/api/chat/stream` 的等价调试别名，便于单独观察 planner 链路。
- `POST /api/chat/reset`：重置会话。
- `GET /api/download/{file_id}`：下载生成的 Word 文档。
- `POST /api/dev/plan-test`：开发快测入口，用需求文本或 state 快速生成 DOCX。

### 管理接口

- `GET /api/skills`：查看 Skill 列表。
- `POST /api/skills/upload`：上传 Skill。
- `GET /api/skills/{skill_name}`：查看 Skill 内容。
- `PUT /api/skills/{skill_name}`：更新 `SKILL.md`。
- `GET /api/bailian/knowledge/status`：查看百炼 RAG 配置状态。
- `GET /api/bailian/files`：查看默认百炼类目下的文件。
- `POST /api/bailian/files/upload`：上传知识文档。
- `GET /api/bailian/files/{file_id}`：查看文件元数据、内容预览和索引解析状态。
- `DELETE /api/bailian/files/{file_id}`：删除远程文件。
- `POST /api/bailian/index/create`：按默认类目创建新索引并更新 `.env`。
- `GET /api/bailian/retrieve`：调试百炼检索结果。

## 协作约定

- 修改前端/UI 相关代码后，需要启动页面进行截图验证，并在回复中附上截图，方便直接评估页面效果。
- 不提交 `backend/.env`、日志、截图、生成的 DOCX 等本地运行产物。
- 本项目已配置本地 Codex Skill：`plan-generator-project-workflow`。后续修改该项目时默认遵守以下流程：
  - 修改前先检查工作树状态，避免覆盖用户或其他任务留下的改动。
  - 前端变更需使用 `frontend-design` 思路进行布局与交互审查，验证后附截图。
  - 后端变更需自动审计是否存在冗余接口、废弃配置、重复服务逻辑、未使用依赖或遗留本地存储路径，并清理明确无用的内容。
  - 验证通过后，除非明确说明“别提交”“别推”或“先别推”，默认提交并推送当前远程分支。

## 开发检查清单

### 前端/UI

- JavaScript 修改后运行：

```bash
node --check frontend/js/app.js
```

- 页面样式或交互修改后，启动服务并截图验证改动区域。临时截图可放在被忽略的 `docs/` 目录，不进入 Git。
- 所有文件上传入口统一使用拖拽上传区样式，避免回退为普通文件输入框。

### 后端/API

- Python 修改后运行针对性语法检查：

```bash
python -m py_compile backend/main.py backend/api/admin_routes.py backend/services/plan_generation.py
```

- 涉及 RAG、Skill、MCP 或文档生成链路时，同步检查 README、依赖、`.gitignore` 和相关服务模块是否仍有过期描述。
- 本项目当前以百炼远程知识库为主，不再保留本地 Qdrant 或 `backend/knowledge/` 文档管理链路，除非后续明确恢复本地向量库方案。

## 扩展方向

- 新增检修类型：在 `backend/skills/` 下新增包含 `SKILL.md` 的目录，并写清触发场景、结构契约、风险项和实施步骤要求。
- 提升知识库可追溯性：上传百炼时同步保留本地或 OSS 原文副本，前端可提供原文下载。
- 增强验证：通过 Page Agent/MCP 对生成方案中的控制台步骤和脚本进行沙箱或测试环境验证。
