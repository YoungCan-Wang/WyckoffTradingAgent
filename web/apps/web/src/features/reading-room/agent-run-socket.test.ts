import { describe, expect, it } from 'vitest'
import { agentRunSocketUrl, parseAgentRunPush } from './agent-run-socket'

describe('agent run push channel', () => {
  it('derives a ws(s) URL from the API base', () => {
    expect(agentRunSocketUrl()).toMatch(/^wss?:\/\/.+\/api\/agent-runs\/ws$/)
  })

  it('parses a pushed record and rejects malformed payloads', () => {
    const record = {
      id: 'run-1',
      kind: 'python_research',
      status: 'completed',
      attempts: 1,
      createdAt: '2026-07-27T00:00:00.000Z',
    }
    expect(parseAgentRunPush(JSON.stringify(record))).toMatchObject({ id: 'run-1', status: 'completed' })
    expect(parseAgentRunPush('not json')).toBeNull()
    expect(parseAgentRunPush(JSON.stringify({ hello: 'world' }))).toBeNull()
    expect(parseAgentRunPush(new ArrayBuffer(4))).toBeNull()
  })
})
