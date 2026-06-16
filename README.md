# 国网云平台检修方案生成器

基于 AgentScope 的检修方案生成工作台。当前版本以对话入口为主：用户输入需求描述或需求文档后，系统先识别意图，再按需抽取关键信息、检索百炼知识库、读取对应 Skill，并生成标准化 Word 检修方案。

## 项目结构

```text
plan-generator-demo/
├── backend/
│   ├── main.py                    # FastAPI 服务入口
│   ├── agents/                    # workflow agent 与 plan agent
│   ├── api/admin_routes.py        # Skill、百炼知识库等管理接口
│   ├── rag/                       # 百炼知识库 Retrieve 与管理封装
│   ├── scripts/generate_plan.py   # Word 文档渲染工具
│   ├── services/                  # 需求抽取、方案生成、Page Agent 等服务
│   ├── skills/                    # AgentScope Skill 目录
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
MODEL_NAME=deepseek-v4-pro
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
  └─ workflow agent
      ├─ 正常聊天：直接回复
      ├─ 生成方案：抽取关键信息，缺失则追问
      ├─ 重新生成：复用已收集需求重新生成
      └─ 修改方案：读取上一版文档，按用户要求修订
          └─ plan agent
              ├─ 初筛并读取 AgentScope Skill
              ├─ 检索百炼 RAG 参考资料
              ├─ 生成文档结构 JSON
              └─ 调用 Word 渲染工具生成 .docx
```

## 核心模块

- **Skill**：每类检修方案的稳定规则、章节结构、风险项、实施步骤约束和工具调用原则放在 `backend/skills/`。
- **百炼 RAG**：历史检修方案、通用模板和阿里云参考资料通过百炼知识库管理，业务生成链路通过 Retrieve API 获取切片。
- **MCP / Page Agent**：`backend/mcp_servers.json` 配置外部 MCP，当前用于 Page Agent 浏览器侧执行与验证。
- **Word 渲染**：`scripts/generate_plan.py` 负责将 plan agent 输出的文档 JSON 渲染为 `.docx`。
- **远程知识库管理**：前端知识库页可上传/查看/删除百炼文档；上传或删除后会自动提交重建索引任务。

## API

### 对话与方案生成

- `POST /api/chat/stream`：主对话入口，SSE 流式返回状态、追问、Agent trace、生成结果。
- `POST /api/chat`：非流式对话入口。
- `POST /api/agent/stream`：实验性 Master ReActAgent 入口，验证单 Agent 自主规划、按需启用工具组、抽取需求、追问和生成 DOCX 的链路；当前不替代稳定的 `/api/chat/stream`。
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
