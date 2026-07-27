import type { UIMessage } from 'ai'
import { describe, expect, it, vi } from 'vitest'
import {
  cancelAgentRun,
  collectSandboxRunTools,
  expiredAgentRun,
  fetchAgentRun,
  isAgentRunTerminal,
  parseAgentRunRecord,
} from './agent-runs'

const queuedRun = {
  id: 'run-1',
  kind: 'python_research',
  status: 'queued',
  attempts: 0,
  createdAt: '2026-07-25T00:00:00.000Z',
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), init)
}

describe('agent run client', () => {
  it('collects only sandbox tool records from assistant messages', () => {
    const messages = [{
      id: 'assistant-1',
      role: 'assistant',
      parts: [
        { type: 'tool-run_python_research', toolCallId: 'tool-1', state: 'output-available', output: queuedRun },
        { type: 'tool-market_overview', toolCallId: 'tool-2', state: 'output-available', output: { status: 'ok' } },
      ],
    }, {
      id: 'user-1',
      role: 'user',
      parts: [{ type: 'text', text: '计算一下' }],
    }] as UIMessage[]

    expect(collectSandboxRunTools(messages)).toEqual([{ runId: 'run-1', toolCallId: 'tool-1', record: queuedRun }])
  })

  it('validates terminal records without treating queued work as completed', () => {
    expect(isAgentRunTerminal(parseAgentRunRecord(queuedRun)!)).toBe(false)
    expect(isAgentRunTerminal(parseAgentRunRecord({ ...queuedRun, status: 'completed', exitCode: 0 })!)).toBe(true)
    expect(parseAgentRunRecord({ id: 'run-1', status: 'completed' })).toBeNull()
  })

  it('reads a user-scoped record through the configured Worker API', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({ ...queuedRun, status: 'running', startedAt: '2026-07-25T00:00:01.000Z' }))

    await expect(fetchAgentRun('run-1', 'access-token', fetcher)).resolves.toMatchObject({ id: 'run-1', status: 'running' })
    expect(fetcher).toHaveBeenCalledWith('http://127.0.0.1:8787/api/agent-runs/run-1', {
      method: 'GET',
      headers: { Authorization: 'Bearer access-token' },
    })
  })

  it('treats a 404 as an expired record instead of an error', async () => {
    const fetcher = vi.fn().mockResolvedValue(jsonResponse({ error: 'Agent run not found' }, { status: 404 }))

    await expect(fetchAgentRun('run-1', 'access-token', fetcher)).resolves.toBeNull()

    const settled = expiredAgentRun(parseAgentRunRecord(queuedRun)!)
    expect(settled.status).toBe('failed')
    expect(isAgentRunTerminal(settled)).toBe(true)
    expect(settled.error).toContain('过期')
  })

  it('cancels only through the Worker API and surfaces its errors', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ ...queuedRun, status: 'cancelled', finishedAt: '2026-07-25T00:00:02.000Z' }))
      .mockResolvedValueOnce(jsonResponse({ error: 'Only queued agent runs can be cancelled' }, { status: 409 }))

    await expect(cancelAgentRun('run-1', 'access-token', fetcher)).resolves.toMatchObject({ status: 'cancelled' })
    await expect(cancelAgentRun('run-1', 'access-token', fetcher)).rejects.toThrow('Only queued agent runs can be cancelled')
    expect(fetcher).toHaveBeenCalledWith('http://127.0.0.1:8787/api/agent-runs/run-1/cancel', {
      method: 'POST',
      headers: { Authorization: 'Bearer access-token' },
    })
  })
})
