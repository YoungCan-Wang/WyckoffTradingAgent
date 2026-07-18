import { describe, expect, it } from 'vitest'
import { AgentRunStore, type AgentRunRecord } from './agent-run-store'

class FakeRedis {
  readonly values = new Map<string, unknown>()

  async set(key: string, value: AgentRunRecord): Promise<void> {
    this.values.set(key, value)
  }

  async get<T>(key: string): Promise<T | null> {
    return (this.values.get(key) as T | undefined) ?? null
  }

  async del(key: string): Promise<number> {
    return this.values.delete(key) ? 1 : 0
  }
}

const record: AgentRunRecord = {
  id: 'run-1',
  kind: 'python_research',
  status: 'completed',
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

  it('removes only the current user record', async () => {
    const redis = new FakeRedis()
    const store = new AgentRunStore(redis)
    await store.save('user-a', record)
    await store.save('user-b', record)
    await store.remove('user-a', record.id)

    expect(await store.get('user-a', record.id)).toBeNull()
    expect(await store.get('user-b', record.id)).toEqual(record)
  })
})
