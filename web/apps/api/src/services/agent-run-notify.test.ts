import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import { notifyAgentRun } from './agent-run-notify'
import type { AgentRunRecord } from './agent-run-store'

const record: AgentRunRecord = {
  id: 'run-1',
  kind: 'python_research',
  status: 'completed',
  attempts: 1,
  createdAt: '2026-07-27T00:00:00.000Z',
}

function fakeNamespace(fetchImpl: () => Promise<Response>) {
  const idFromName = vi.fn((name: string) => name)
  const stubFetch = vi.fn(fetchImpl)
  const namespace = { idFromName, get: () => ({ fetch: stubFetch }) } as unknown as DurableObjectNamespace
  return { namespace, idFromName, stubFetch }
}

describe('notifyAgentRun', () => {
  it('does nothing without the notifier binding', async () => {
    await expect(notifyAgentRun({} as Env, 'user-1', record)).resolves.toBeUndefined()
  })

  it('posts the record to the per-user notifier object', async () => {
    const { namespace, idFromName, stubFetch } = fakeNamespace(async () => Response.json({ delivered: 1 }))

    await notifyAgentRun({ AGENT_RUN_NOTIFIER: namespace }, 'user-1', record)

    expect(idFromName).toHaveBeenCalledWith('user-1')
    expect(stubFetch).toHaveBeenCalledWith('https://agent-run-notifier/notify', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify(record),
    }))
  })

  it('swallows delivery failures so run state is never affected', async () => {
    const { namespace } = fakeNamespace(async () => { throw new Error('durable object unavailable') })
    await expect(notifyAgentRun({ AGENT_RUN_NOTIFIER: namespace }, 'user-1', record)).resolves.toBeUndefined()
  })
})
