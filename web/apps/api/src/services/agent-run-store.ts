import { Redis } from '@upstash/redis/cloudflare'
import type { Env } from '../app'

export type AgentRunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'

export type AgentRunRecord = {
  id: string
  kind: 'python_research'
  status: AgentRunStatus
  attempts: number
  createdAt: string
  startedAt?: string
  finishedAt?: string
  cancelledAt?: string
  exitCode?: number
  stdout?: string
  stderr?: string
  error?: string
  lastError?: string
  usage?: {
    activeCpuUsageMs: number
    networkIngressBytes: number
    networkEgressBytes: number
  }
}

type RedisScript<T> = { eval: (keys: string[], args: string[]) => Promise<T> }

type RedisClient = {
  set: {
    (key: string, value: unknown, options: { ex: number }): Promise<unknown>
    (key: string, value: unknown, options: { ex: number; nx: true }): Promise<unknown>
  }
  get: <T>(key: string) => Promise<T | null>
  del: (key: string) => Promise<number>
  createScript: <T>(script: string) => RedisScript<T>
}

const LEASE_SECONDS = 180
const TRANSITION_SCRIPT = `
local current = redis.call('GET', KEYS[1])
if not current then return nil end
local record = cjson.decode(current)
if record.status ~= ARGV[1] then return nil end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return ARGV[2]`

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

  async cancel(userId: string, runId: string): Promise<AgentRunRecord | null> {
    const record = await this.get(userId, runId)
    if (!record) return null
    return this.transition(userId, record, 'queued', {
      ...record,
      status: 'cancelled',
      cancelledAt: new Date().toISOString(),
    })
  }

  async claim(userId: string, runId: string): Promise<AgentRunRecord | null> {
    const record = await this.get(userId, runId)
    if (!record || !isClaimable(record.status)) return null
    return this.transition(userId, record, record.status, {
      ...record,
      status: 'running',
      attempts: record.attempts + 1,
      startedAt: record.startedAt || new Date().toISOString(),
      lastError: undefined,
    })
  }

  async requeue(userId: string, record: AgentRunRecord, error: string): Promise<AgentRunRecord | null> {
    return this.transition(userId, record, 'running', {
      ...record,
      status: 'queued',
      lastError: error,
    })
  }

  async fail(userId: string, record: AgentRunRecord, error: string): Promise<AgentRunRecord | null> {
    if (!isClaimable(record.status)) return null
    return this.transition(userId, record, record.status, {
      ...record,
      status: 'failed',
      error,
      finishedAt: new Date().toISOString(),
    })
  }

  async acquireLease(userId: string, runId: string): Promise<boolean> {
    return Boolean(await this.redis.set(leaseKey(userId, runId), '1', { ex: LEASE_SECONDS, nx: true }))
  }

  async releaseLease(userId: string, runId: string): Promise<void> {
    await this.redis.del(leaseKey(userId, runId))
  }

  private async transition(
    userId: string,
    record: AgentRunRecord,
    expected: AgentRunStatus,
    next: AgentRunRecord,
  ): Promise<AgentRunRecord | null> {
    const script = this.redis.createScript<AgentRunRecord | null>(TRANSITION_SCRIPT)
    return script.eval([runKey(userId, record.id)], [
      expected,
      JSON.stringify(next),
      String(this.ttlSeconds),
    ])
  }
}

export function createAgentRunStore(env: Env): AgentRunStore | null {
  const url = env.UPSTASH_REDIS_REST_URL?.trim()
  const token = env.UPSTASH_REDIS_REST_TOKEN?.trim()
  if (!url && !token) return null
  if (!url || !token) throw new Error('Upstash Redis env is incomplete')
  return new AgentRunStore(new Redis({ url, token }), runTtl(env.AGENT_RUN_TTL_SECONDS))
}

function isClaimable(status: AgentRunStatus): status is 'queued' | 'running' {
  return status === 'queued' || status === 'running'
}

function runKey(userId: string, runId: string): string {
  return `wyckoff:agent-run:${encodeURIComponent(userId)}:${encodeURIComponent(runId)}`
}

function leaseKey(userId: string, runId: string): string {
  return `${runKey(userId, runId)}:lease`
}

function runTtl(raw: string | undefined): number {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed < 60) return 3600
  return Math.min(Math.trunc(parsed), 86_400)
}
