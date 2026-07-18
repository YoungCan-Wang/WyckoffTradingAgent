import { Redis } from '@upstash/redis/cloudflare'
import type { Env } from '../app'

export type AgentRunStatus = 'running' | 'completed' | 'failed'

export type AgentRunRecord = {
  id: string
  kind: 'python_research'
  status: AgentRunStatus
  createdAt: string
  finishedAt?: string
  exitCode?: number
  stdout?: string
  stderr?: string
  error?: string
  usage?: {
    activeCpuUsageMs: number
    networkIngressBytes: number
    networkEgressBytes: number
  }
}

type RedisClient = {
  set: (key: string, value: AgentRunRecord, options: { ex: number }) => Promise<unknown>
  get: <T>(key: string) => Promise<T | null>
  del: (key: string) => Promise<number>
}

export class AgentRunStore {
  constructor(private readonly redis: RedisClient, private readonly ttlSeconds = 3600) {}

  async save(userId: string, record: AgentRunRecord): Promise<void> {
    await this.redis.set(runKey(userId, record.id), record, { ex: this.ttlSeconds })
  }

  get(userId: string, runId: string): Promise<AgentRunRecord | null> {
    return this.redis.get<AgentRunRecord>(runKey(userId, runId))
  }

  async remove(userId: string, runId: string): Promise<void> {
    await this.redis.del(runKey(userId, runId))
  }
}

export function createAgentRunStore(env: Env): AgentRunStore | null {
  const url = env.UPSTASH_REDIS_REST_URL?.trim()
  const token = env.UPSTASH_REDIS_REST_TOKEN?.trim()
  if (!url && !token) return null
  if (!url || !token) throw new Error('Upstash Redis env is incomplete')
  return new AgentRunStore(new Redis({ url, token }), runTtl(env.AGENT_RUN_TTL_SECONDS))
}

function runKey(userId: string, runId: string): string {
  return `wyckoff:agent-run:${encodeURIComponent(userId)}:${encodeURIComponent(runId)}`
}

function runTtl(raw: string | undefined): number {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed < 60) return 3600
  return Math.min(Math.trunc(parsed), 86_400)
}
