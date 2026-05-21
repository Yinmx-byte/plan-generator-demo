# 国网云平台检修方案生成器

根据检修需求自动生成标准化检修方案 Word 文档。

## 项目结构

```
plan-generator-demo/
├── backend/
│   ├── main.py                  # FastAPI 服务入口（基于 AgentScope 框架）
│   ├── rag/
│   │   └── knowledge_base.py    # 基于 AgentScope RAG 的知识库模块
│   ├── scripts/
│   │   └── generate_plan.py     # Skill 结构 JSON → Word 文档生成脚本
│   ├── skills/
│   │   ├── maintenance-plan-composer/   # 通用编排 Skill
│   │   ├── ecs-instance-provisioning/   # ECS 创建/配置 Skill
│   │   ├── database-maintenance-plan/   # 数据库检修 Skill
│   │   ├── component-scaling-plan/      # 扩缩容 Skill
│   │   ├── restart-maintenance-plan/    # 维护性重启 Skill
│   │   └── generic-maintenance-plan/    # 兜底 Skill
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html               # 前端表单页面
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
cp .env.example .env
# 编辑 .env，填入你的 ANTHROPIC_API_KEY
# 可选：填入 OPENAI_API_KEY / EMBEDDING_MODEL_NAME 启用 RAG
```

### 3. 启动服务

```bash
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 4. 打开浏览器

访问 `http://localhost:8000`，填写表单，点击"生成检修方案"。

## 工作流程

```
前端表单 ──POST──> FastAPI
                 ├── 注册 Skill 元数据（name/description）
                 ├── 根据检修类型按需展开多个 Skill
                 ├── 可选 RAG 检索 Skill 知识
                 └── AnthropicChatModel (AgentScope) ──> document.sections JSON ──> generate_plan.py ──> .docx 下载
```

- **Skill 加载** 遵循 AgentScope 渐进式披露原则：启动时只暴露 `name/description`，生成时再展开命中的 `SKILL.md`
- **多 Skill 路由** 支持一个检修需求同时命中 ECS、数据库、扩缩容、重启等多个类型 Skill
- **RAG 模块** 基于 AgentScope 1.0 `SimpleKnowledge` / `QdrantStore` / `OpenAITextEmbedding`，默认索引 Skill 目录下的 Markdown 知识
- **AnthropicChatModel** 是 AgentScope 1.0 对 Claude API 的封装，负责模型调用
- **generate_plan.py** 只负责渲染 Skill 输出的 `document.sections`，不再硬编码检修方案章节

## API

### POST /api/generate

接收表单数据，返回 .docx 文件。

主要参数：

| 字段 | 说明 |
|------|------|
| background | 检修背景，每行一条 |
| maintenance_type | 检修类型 |
| network | 内网/外网 |
| instances | 实例描述 |
| schedule_* | 检修窗口 |
| provider/executor/reviewer/security_officer | 人员 |
| ascm_account/bastion_account | 授权账号 |
| ops_detail | 操作步骤描述 |
| tech_params | 技术参数（JSON） |

### GET /api/health

健康检查。

### GET /api/rag/retrieve

调试 RAG 检索结果。示例：

```bash
curl "http://localhost:8000/api/rag/retrieve?query=检修方案"
```

## 扩展方向

- **新增检修类型**：在 `backend/skills/` 下新增一个包含 `SKILL.md` 的目录，并在 description 里写清触发场景
- **知识库**：在各 Skill 的 `SKILL.md` 或 references/*.md 中加入更多领域知识（如标准操作流程、常见配置参数等），RAG 会索引这些 Markdown 文件
- **多 Agent**：引入 AgentScope ReActAgent 实现多角色协作（方案编写→复核→修正）
- **MCP**：通过 MCP 协议接入外部工具（如 CMDB 查询实例信息）
- **文档结构扩展**：优先调整 Skill 中的 `document.sections` 输出要求，而不是修改 Python 脚本
- **前端优化**：将表单改为分步向导，动态添加实例表格
