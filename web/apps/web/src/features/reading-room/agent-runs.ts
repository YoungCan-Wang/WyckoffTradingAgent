import { z } from 'zod'
import type { UIMessage } from 'ai'
import { apiUrl } from '@/lib/api-url'
import { isToolPart, type MessagePart } from './messages'

const agentRunStatusSchema = z.enum(['queued', 'running', 'completed', 'failed', 'cancelled'])

const agentRunSchema = z.object({
  id: z.string().min(1),
  kind: z.literal('python_research'),
  status: agentRunStatusSchema,
  attempts: z.number().int().nonnegative(),
  createdAt: z.string(),
  startedAt: z.string().optional(),
  finishedAt: z.string().optional(),
  exitCode: z.number().int().optional(),
  stdout: z.string().optional(),
  stderr: z.string().optional(),
  error: z.string().optional(),
  usage: z.object({
    activeCpuUsageMs: z.number().nonnegative(),
    networkIngressBytes: z.number().nonnegative(),
    networkEgressBytes: z.number().nonnegative(),
  }).optional(),
})

const apiErrorSchema = z.object({ error: z.string() })

export type AgentRunRecord = z.infer<typeof agentRunSchema>
export type AgentRunStatus = z.infer<typeof agentRunStatusSchema>

export type SandboxRunTool = {
  runId: string
  toolCallId: string
  record: AgentRunRecord
}

export function parseAgentRunRecord(value: unknown): AgentRunRecord | null {
  const parsed = agentRunSchema.safeParse(value)
  return parsed.success ? parsed.data : null
}

export function isAgentRunTerminal(record: AgentRunRecord): boolean {
  return record.status === 'completed' || record.status === 'failed' || record.status === 'cancelled'
}

export function collectSandboxRunTools(messages: UIMessage[]): SandboxRunTool[] {
  const tools: SandboxRunTool[] = []
  for (const message of messages) {
    if (message.role !== 'assistant') continue
    for (const part of message.parts as MessagePart[]) {
      if (!isToolPart(part) || toolName(part) !== 'run_python_research') continue
      const record = parseAgentRunRecord(part.output)
      if (record) tools.push({ runId: record.id, toolCallId: part.toolCallId, record })
    }
  }
  return tools
}

export async function fetchAgentRun(
  runId: string,
  accessToken: string,
  fetcher: typeof fetch = fetch,
): Promise<AgentRunRecord> {
  return requestAgentRun(`/api/agent-runs/${runId}`, 'GET', accessToken, fetcher)
}

export async function cancelAgentRun(
  runId: string,
  accessToken: string,
  fetcher: typeof fetch = fetch,
): Promise<AgentRunRecord> {
  return requestAgentRun(`/api/agent-runs/${runId}/cancel`, 'POST', accessToken, fetcher)
}

async function requestAgentRun(
  path: `/api/agent-runs/${string}`,
  method: 'GET' | 'POST',
  accessToken: string,
  fetcher: typeof fetch,
): Promise<AgentRunRecord> {
  const response = await fetcher(apiUrl(path), {
    method,
    headers: { Authorization: `Bearer ${accessToken}` },
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const error = apiErrorSchema.safeParse(payload)
    throw new Error(error.success ? error.data.error : '隔离任务服务请求失败')
  }
  const record = parseAgentRunRecord(payload)
  if (!record) throw new Error('隔离任务服务返回数据不完整，请稍后重试')
  return record
}

function toolName(part: MessagePart): string {
  return part.type === 'dynamic-tool' ? String(part.toolName || '') : part.type.slice(5)
}
