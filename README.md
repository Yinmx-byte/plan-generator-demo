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
│   ├── quality_iterator/          # 项目内置检修 Skill 质量迭代脚本
│   ├── mcp_servers/page_agent/    # 项目内置 Page Agent MCP 适配包
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

Page Agent MCP 已内置在项目目录中，首次使用浏览器验证前安装它的 Node 依赖：

```bash
cd backend/mcp_servers/page_agent
npm install
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

# 方案归档。RDS 存储索引与审计记录，OSS 存储 DOCX、快照和版本差异。
ARCHIVE_RDS_HOST=rm-xxx.mysql.rds.aliyuncs.com
ARCHIVE_RDS_PORT=3306
ARCHIVE_RDS_DATABASE=generated_maintenance_plan
ARCHIVE_RDS_USERNAME=archive_user
ARCHIVE_RDS_PASSWORD=xxx
PLAN_ARCHIVE_OSS_BUCKET=your-private-bucket
PLAN_ARCHIVE_OSS_REGION=cn-beijing
PLAN_ARCHIVE_OSS_PREFIX=maintenance-plan-archive

# Skill 远程版本库。复用上面的 RDS，完整 Skill 包存入独立 OSS Bucket。
ITERATED_SKILL_OSS_BUCKET=iterated-skill
ITERATED_SKILL_OSS_REGION=cn-beijing
ITERATED_SKILL_OSS_PREFIX=skill-versions
```

Skill 质量迭代使用同一 RDS 中的 `quality_reference_documents` 表保存优质方案元数据，原始 DOCX 存放在独立的私有 OSS Bucket。配置示例：

```env
QUALITY_REFERENCE_OSS_BUCKET=high-quality-plan
QUALITY_REFERENCE_OSS_REGION=cn-beijing
QUALITY_REFERENCE_OSS_PREFIX=quality-references
QUALITY_REFERENCE_TOP_K=5
QUALITY_EVALUATOR_MODEL_NAME=deepseek-v4-pro
QUALITY_EVALUATOR_MAX_TOKENS=6000
```

首次导入分类后的历史方案：

```powershell
python quality_iterator/scripts/import_quality_references.py "D:\个人工作\2026\微创项目\整理后文档\检修方案-整理"
```

评估总分由确定性规则（40%）、同产品同动作优质文档对比（30%）和独立评估 Agent（30%）组成。产品 Skill 不参与定义自己的评分标准，只接收评估缺陷形成的候选修改。

### 3. 统一启动（推荐）

在项目根目录选择启动目标：

```powershell
# 同时启动业务后端和 AgentScope Studio
.\start.cmd all

# 仅启动业务后端
.\start.cmd backend

# 仅启动 AgentScope Studio
.\start.cmd studio
```

启动脚本会优先使用当前 Python 环境，并可从 Conda 环境清单自动定位 `plan-generator`。不带参数执行 `.\start.cmd` 时默认使用 `all`。需要自定义端口或关闭后端热重载时，直接调用 PowerShell 入口：

```powershell
.\start.ps1 -Target all -BackendPort 8000 -StudioPort 3000 -NoReload
```

`all` 模式会复用已在目标端口运行的 Studio，并在退出时清理本次命令启动的子进程。

### 4. 手动启动后端

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000`。

### 5. 手动启动 AgentScope Studio（可选）

Studio 是独立的开发观测页面，不会把底层 Prompt、模型调用和工具参数直接展示在业务前端。首次使用先安装：

```powershell
npm install -g @agentscope/studio
```

启动 Studio：

```powershell
.\start.ps1 -Target studio
```

浏览器访问 `http://127.0.0.1:3000`，再启动后端并发起一次对话。Studio 中可查看 Agent、LLM、工具、格式化器调用层级、耗时和异常。后端连接状态可通过 `GET /api/observability/status` 检查。若不需要追踪，将 `AGENTSCOPE_OBSERVABILITY_ENABLED=false` 即可，现有 SSE 流程不受影响。

### 6. 业务页面轻量追踪

启用 AgentScope 追踪后，主对话的每轮回答下方会显示“查看完整追踪”。该视图面向业务演示，仅展示本轮工作流、Agent、模型、工具、格式化与检索节点的层级、状态、耗时和 Token 汇总；不会返回 Prompt、用户文档正文、工具参数或工具结果。

轻量追踪暂存在后端内存中，默认最多保留 80 条、保留 1 小时，服务重启后清空。可通过 `LOCAL_TRACE_MAX_TRACES` 和 `LOCAL_TRACE_TTL_SECONDS` 调整。接口如下：

```text
GET /api/observability/traces/{trace_id}
```

业务页面轻量追踪用于快速说明“本轮做了什么”；AgentScope Studio 用于开发人员查看更完整的原生 OpenTelemetry Trace。

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

- **Skill**：每类检修方案的稳定规则、章节结构、风险项、实施步骤约束和工具调用原则以 OSS 为远程版本源，RDS 的 `iterated_skill_versions` 表保存版本元数据；`backend/.runtime_skills/` 是 AgentScope 运行时缓存，`backend/skills/` 仅作为首次启动的仓库种子。
- **百炼 RAG**：历史检修方案、通用模板和阿里云参考资料通过百炼知识库管理，业务生成链路通过 Retrieve API 获取切片。
- **阿里云只读资源查询**：通过阿里云官方 Python SDK 查询 ECS/VPC/VSwitch/实例归属信息、CloudMonitor 使用率指标和 Resource Center 可访问产品统计。`cloud_query` 工具组已拆分为资源清单、监控指标、资源组产品统计三类工具，指标名称和中文别名由 `backend/config/cloud_query_catalog.yaml` 统一配置，用于智能问数、生成前资源核对和验证前检查。
- **MCP / Page Agent**：`backend/mcp_servers/page_agent/` 内置了项目使用的 Page Agent MCP 适配包，支持 `execute_task_async` 与 `get_task_events`，前端可流式展示浏览器自动化过程；`backend/mcp_servers.json` 默认指向该项目内路径，不依赖外部 Page Agent 项目。
- **质量自迭代**：`backend/quality_iterator/scripts/` 内置检修 Skill 质量迭代脚本，先生成候选 DOCX，再与高质量历史方案对比，把文档缺陷反推为 Skill 候选更新。
- **方案生成服务**：`services/plan_generation.py` 将写文档能力封装为 `compose_plan_json`、`validate_plan_json`、`render_docx` 三步；其中 `compose_plan_json` 内部仍使用专用 Plan ReActAgent，方便加载 Skill 和调用渲染检查工具。
- **Word 渲染**：`scripts/generate_plan.py` 将已验证的内容 JSON 与通用格式契约合成为 `.docx`；默认格式位于 `skills/maintenance-plan-composer/references/document-style.json`，所有检修类型共用，渲染工具也支持显式传入 `style_contract`。
- **远程知识库管理**：前端知识库页可上传/查看/删除百炼文档；上传或删除后会自动提交重建索引任务。
- **方案归档**：`services/remote_plan_archive.py` 以 RDS 保存归档元数据、版本和审计记录，以 OSS 保存 DOCX、方案快照和版本差异。只有主对话生成的正式方案在下载时自动归档；质量迭代、开发快测等候选文档不会进入归档。归档文件可通过 `/api/archive/files/{record_id}/download` 下载。

## Master Agent 工具组配置

Master Agent 的工具实现位于 `backend/tools/`，由 `backend/tools/master_toolkit.py` 统一注册。默认注册 `context`、`planning`、`generation`、`document`、`cloud_query`、`history` 六个工具组；如需临时收窄工具面，可设置：

```env
MASTER_AGENT_TOOL_GROUPS=context,planning,generation,document,cloud_query,history
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
- `POST /api/dev/plan-test`：开发与质量迭代快测入口，用需求文本或 state 快速生成候选 DOCX；该入口生成的文档不会进入正式方案归档。
- `GET /api/cloud/inventory`：通过配置化工具层只读查询 ECS/VPC/VSwitch/实例归属等资源清单。
- `GET /api/cloud/metrics`：通过配置化指标 key 查询 ECS CPU、内存、磁盘、网络等 CloudMonitor 指标。
- `GET /api/cloud/resource-products`：通过 Resource Center 统计当前账号或指定资源组可访问资源涉及的产品类别。
- `GET /api/cloud/ecs-vpc-info`：旧版兼容入口，仍可查询 ECS/VPC/VSwitch/实例归属和使用率信息。
- `GET /api/dev/cloud-query-test`：开发验证接口，默认验证产品/指标映射、缺参追问和资源产品聚合；设置 `run_live=true` 后会在当前环境有 AK/SK 时执行真实只读云查询。
- `POST /api/page-agent/task/stream`：通过项目内置 Page Agent MCP 执行浏览器任务，并流式返回中间事件。
- `POST /api/skill-iterator/run`：调用项目内置质量迭代脚本，生成候选 DOCX 后执行三段式质量评估。

### 管理接口

- `GET /api/skills`：查看 Skill 列表。
- `POST /api/skills/upload`：上传 Skill。
- `GET /api/skills/{skill_name}`：查看 Skill 内容。
- `PUT /api/skills/{skill_name}`：更新 `SKILL.md`。
- `GET /api/skills/{skill_name}/versions`：查看 RDS 中的远程 Skill 版本记录。
- `POST /api/skills/{skill_name}/rollback`：从 OSS 恢复指定完整 Skill 包，并将回退结果重新发布为当前版本。
- `GET /api/skill-storage/status`：查看远程 Skill 数据表、OSS Bucket、版本数和当前生效 Skill 数。
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
- 质量迭代页的产品示例参数统一维护在 `frontend/data/iterator-samples.json`，新增检修方案 Skill 时应同步增加对应示例。

### 后端/API

- Python 修改后运行针对性语法检查：

```bash
python -m py_compile backend/main.py backend/api/admin_routes.py backend/services/plan_generation.py
```

- 涉及 RAG、Skill、MCP、质量迭代或文档生成链路时，同步检查 README、依赖、`.gitignore` 和相关服务模块是否仍有过期描述。
- 本项目当前以百炼远程知识库为主，不再保留本地 Qdrant 或 `backend/knowledge/` 文档管理链路，除非后续明确恢复本地向量库方案。

## 扩展方向

- 新增检修类型：在 `backend/skills/` 下新增包含 `SKILL.md` 的目录，并写清触发场景、结构契约、风险项和实施步骤要求。
- 提升知识库可追溯性：上传百炼时同步保留本地或 OSS 原文副本，前端可提供原文下载。
- 增强验证：通过 Page Agent/MCP 对生成方案中的控制台步骤和脚本进行沙箱或测试环境验证。
