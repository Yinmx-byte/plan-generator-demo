#!/usr/bin/env node
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { exec } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { platform } from 'node:os'
import * as z from 'zod/v4'

import { HubBridge } from './hub-bridge.js'

const env = process.env
const port = parseInt(env.PORT || '38401')
const { version } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

/** @type {Record<string, string>} */
const llmConfig = {}
if (env.LLM_BASE_URL) llmConfig.baseURL = env.LLM_BASE_URL
if (env.LLM_MODEL_NAME) llmConfig.model = env.LLM_MODEL_NAME
if (env.LLM_API_KEY) llmConfig.apiKey = env.LLM_API_KEY

// --- Hub bridge (HTTP + WebSocket) ---

const hub = new HubBridge(port)
await hub.start()
const asyncTasks = new Map()

function pageAgentConfig() {
	return Object.keys(llmConfig).length > 0 ? llmConfig : undefined
}

function startAsyncTask(task) {
	const taskId = randomUUID()
	const record = {
		id: taskId,
		status: 'running',
		events: [],
		result: null,
		error: null,
		createdAt: Date.now(),
		updatedAt: Date.now(),
	}
	asyncTasks.set(taskId, record)

	hub.executeTask(task, pageAgentConfig(), (trace) => {
		record.events.push(trace)
		record.updatedAt = Date.now()
	})
		.then((result) => {
			record.status = result.success ? 'completed' : 'failed'
			record.result = result
			record.updatedAt = Date.now()
		})
		.catch((err) => {
			record.status = 'error'
			record.error = err instanceof Error ? err.message : String(err)
			record.updatedAt = Date.now()
		})

	return record
}

// Open launcher in default browser
const url = `http://localhost:${port}`
const cmd = platform() === 'darwin' ? 'open' : platform() === 'win32' ? 'start ""' : 'xdg-open'
exec(`${cmd} "${url}"`, (err) => {
	if (err) console.error(`[page-agent-mcp] Could not open browser: ${err.message}`)
})

// --- MCP server (stdio) ---

const mcpServer = new McpServer({ name: 'page-agent', version })

mcpServer.registerTool(
	'execute_task',
	{
		description: "Execute a task in user's browser.",
		inputSchema: {
			task: z
				.string()
				.describe(
					'Task description. Give specific instructions for the task. Steps preferable. And the information you want to get after the task is done.'
				),
		},
	},
	async ({ task }) => {
		try {
			const result = await hub.executeTask(task, pageAgentConfig())
			return {
				content: [
					{
						type: 'text',
						text: result.success
							? `Task completed.\n\n${result.data}`
							: `Task failed.\n\n${result.data}`,
					},
				],
			}
		} catch (err) {
			return {
				content: [{ type: 'text', text: `Error: ${err.message}` }],
				isError: true,
			}
		}
	}
)

mcpServer.registerTool(
	'execute_task_async',
	{
		description:
			'Start a browser task and return a task id immediately. Use get_task_events to stream status, activity, history, and final result.',
		inputSchema: {
			task: z
				.string()
				.describe(
					'Task description. Give specific instructions for the task. Steps preferable. And the information you want to get after the task is done.'
				),
		},
	},
	async ({ task }) => {
		try {
			if (!hub.connected) throw new Error('Hub is not connected. Is the extension running?')
			if (hub.busy) throw new Error('Agent is already running a task.')
			const record = startAsyncTask(task)
			return {
				content: [
					{
						type: 'text',
						text: JSON.stringify(
							{
								task_id: record.id,
								status: record.status,
								cursor: 0,
							},
							null,
							2
						),
					},
				],
			}
		} catch (err) {
			return {
				content: [{ type: 'text', text: `Error: ${err.message}` }],
				isError: true,
			}
		}
	}
)

mcpServer.registerTool(
	'get_task_events',
	{
		description: 'Read Page Agent task events since a cursor for a task started by execute_task_async.',
		inputSchema: {
			task_id: z.string().describe('Task id returned by execute_task_async.'),
			cursor: z.number().optional().describe('Event cursor returned by previous get_task_events call.'),
		},
	},
	async ({ task_id, cursor = 0 }) => {
		const record = asyncTasks.get(task_id)
		if (!record) {
			return {
				content: [{ type: 'text', text: `Error: Unknown task id ${task_id}` }],
				isError: true,
			}
		}
		const start = Math.max(0, Number(cursor) || 0)
		const events = record.events.slice(start)
		return {
			content: [
				{
					type: 'text',
					text: JSON.stringify(
						{
							task_id,
							status: record.status,
							cursor: start,
							next_cursor: start + events.length,
							events,
							result: record.result,
							error: record.error,
							busy: hub.busy,
							updated_at: record.updatedAt,
						},
						null,
						2
					),
				},
			],
		}
	}
)

mcpServer.registerTool(
	'get_status',
	{
		description: 'Check the current status of the Page Agent hub.',
	},
	async () => ({
		content: [
			{
				type: 'text',
				text: JSON.stringify({ connected: hub.connected, busy: hub.busy }, null, 2),
			},
		],
	})
)

mcpServer.registerTool(
	'stop_task',
	{
		description: 'Stop the currently running browser automation task.',
	},
	async () => {
		hub.stopTask()
		return { content: [{ type: 'text', text: 'Stop signal sent.' }] }
	}
)

const transport = new StdioServerTransport()
await mcpServer.connect(transport)
console.error('[page-agent-mcp] MCP server ready (stdio)')
