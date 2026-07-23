import { env as workerEnv } from 'cloudflare:workers'
import { describe, expect, it } from 'vitest'
import type { Env } from '../app'
import { AgentRunStore, createAgentRunStore, type AgentRunRecord } from './agent-run-store'

class FakeRedis {
  readonly values = new Map<string, unknown>()

  async set(key: string, value: unknown, options: { ex: number; nx?: true }): Promise<unknown> {
    if (options.nx && this.values.has(key)) return null
    this.values.set(key, value)
    return 'OK'
  }

  async get<T>(key: string): Promise<T | null> {
    return (this.values.get(key) as T | undefined) ?? null
  }

  async del(key: string): Promise<number> {
    return this.values.delete(key) ? 1 : 0
  }

  createScript<T>(_script: string): { eval: (keys: string[], args: string[]) => Promise<T> } {
    return {
      eval: async ([key], [expected, next]) => {
        const current = this.values.get(key) as AgentRunRecord | undefined
        if (!current || current.status !== expected) return null as T
        const record = JSON.parse(next) as AgentRunRecord
        this.values.set(key, record)
        return record as T
      },
    }
  }
}

const record: AgentRunRecord = {
  id: 'run-1',
  kind: 'python_research',
  status: 'queued',
  attempts: 0,
  createdAt: '2026-07-18T00:00:00.000Z',
}

describe('Agent run store', () => {
  it('isolates records by authenticated user', async () => {
    const redis = new FakeRedis()
    const store = new AgentRunStore(redis)
    await store.save('user-a', record)

    expect(await store.get('user-a', record.id)).toEqual(record)
    expect(await store.get('user-b', record.id)).toBeNull()
  })

  it('cancels only a queued run', async () => {
    const store = new AgentRunStore(new FakeRedis())
    await store.save('user-a', record)

    expect(await store.cancel('user-a', record.id)).toMatchObject({ status: 'cancelled' })
    expect(await store.cancel('user-a', record.id)).toBeNull()
  })

  it('claims, requeues, and increments attempts atomically', async () => {
    const store = new AgentRunStore(new FakeRedis())
    await store.save('user-a', record)
    const claimed = await store.claim('user-a', record.id)

    expect(claimed).toMatchObject({ status: 'running', attempts: 1 })
    expect(await store.requeue('user-a', claimed!, 'Sandbox execution failed')).toMatchObject({
      status: 'queued',
      attempts: 1,
      lastError: 'Sandbox execution failed',
    })
    expect(await store.claim('user-a', record.id)).toMatchObject({ status: 'running', attempts: 2 })
  })

  it('uses a lease to prevent two consumers from executing the same run', async () => {
    const store = new AgentRunStore(new FakeRedis())

    expect(await store.acquireLease('user-a', record.id)).toBe(true)
    expect(await store.acquireLease('user-a', record.id)).toBe(false)
    await store.releaseLease('user-a', record.id)
    expect(await store.acquireLease('user-a', record.id)).toBe(true)
  })

  it('removes only a terminal record for the current user when asked by the route', async () => {
    const redis = new FakeRedis()
    const store = new AgentRunStore(redis)
    await store.save('user-a', { ...record, status: 'completed' })
    await store.save('user-b', { ...record, status: 'completed' })
    await store.remove('user-a', record.id)

    expect(await store.get('user-a', record.id)).toBeNull()
    expect(await store.get('user-b', record.id)).toMatchObject({ status: 'completed' })
  })
})

type IntegrationEnv = Env & { RUN_UPSTASH_INTEGRATION?: string }

const integrationEnv = workerEnv as IntegrationEnv
const describeUpstash = integrationEnv.RUN_UPSTASH_INTEGRATION === '1' ? describe : describe.skip

describeUpstash('Upstash agent run store integration', () => {
  it('applies the compare-and-set transitions against the real Redis API', async () => {
    const store = createAgentRunStore(integrationEnv)
    if (!store) throw new Error('Upstash store is unavailable')
    const userId = `agent-run-integration-${crypto.randomUUID()}`
    const liveRecord = { ...record, id: crypto.randomUUID() }
    try {
      await store.save(userId, liveRecord)
      const claimed = await store.claim(userId, liveRecord.id)
      expect(claimed).toMatchObject({ status: 'running', attempts: 1 })
      const requeued = await store.requeue(userId, claimed!, 'Sandbox execution failed')
      expect(requeued).toMatchObject({ status: 'queued', lastError: 'Sandbox execution failed' })
      expect(await store.cancel(userId, liveRecord.id)).toMatchObject({ status: 'cancelled' })
    } finally {
      await store.remove(userId, liveRecord.id)
    }
  })
})
