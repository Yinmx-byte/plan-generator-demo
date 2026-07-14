# 项目内置 Page Agent MCP

该目录是 `plan-generator-demo` 内置的 Page Agent MCP 适配包，用于通过 Chrome Page Agent 扩展执行浏览器侧验证任务。

## 使用方式

首次使用前在本目录安装依赖：

```bash
npm install
```

后端通过 `backend/mcp_servers.json` 启动该 MCP：

```json
{
  "name": "page-agent",
  "type": "stdio",
  "command": "node",
  "args": ["src/index.js"],
  "cwd": "mcp_servers/page_agent"
}
```

## 工具

- `execute_task`：阻塞式执行浏览器任务。
- `execute_task_async`：启动任务并立即返回 `task_id`。
- `get_task_events`：按 cursor 拉取 Page Agent 中间事件，用于前端流式展示。
- `get_status`：查看 Hub 是否连接、是否忙碌。
- `stop_task`：中止当前浏览器任务。

## 环境变量

- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL_NAME`
- `PAGE_AGENT_PORT`

这些变量由 `backend/mcp_servers.json` 从后端环境传入。
