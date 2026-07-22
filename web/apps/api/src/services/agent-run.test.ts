import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import type { AgentRunStore } from './agent-run-store'
import { AgentRunServiceError, runPythonResearch } from './agent-run'

function testStore(save = vi.fn(async () => undefined)): AgentRunStore {
  return { save } as unknown as AgentRunStore
}

describe('Agent run service', () => {
  it('persists the running and completed records around one sandbox execution', async () => {
    const save = vi.fn(async () => undefined)
    const log = vi.fn()
    const executeSandbox = vi.fn(async () => ({
      exitCode: 0,
      stdout: '42\n',
      stderr: '',
      activeCpuUsageMs: 17,
      networkIngressBytes: 0,
      networkEgressBytes: 0,
    }))

    const record = await runPythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => testStore(save), executeSandbox, log, requestId: 'request-1' },
    )

    expect(record).toMatchObject({ kind: 'python_research', status: 'completed', stdout: '42\n' })
    expect(save).toHaveBeenCalledTimes(2)
    expect(save.mock.calls[0]).toEqual(['user-1', expect.objectContaining({ status: 'running' })])
    expect(save.mock.calls[1]).toEqual(['user-1', expect.objectContaining({ status: 'completed' })])
    expect(executeSandbox).toHaveBeenCalledWith(
      expect.anything(),
      'print(42)',
      expect.objectContaining({ requestId: 'request-1', runId: expect.any(String) }),
    )
    expect(log).toHaveBeenNthCalledWith(1, 'started', expect.objectContaining({ requestId: 'request-1' }))
    expect(log).toHaveBeenNthCalledWith(2, 'finished', expect.objectContaining({
      requestId: 'request-1',
      status: 'completed',
      usage: { activeCpuUsageMs: 17, networkIngressBytes: 0, networkEgressBytes: 0 },
    }))
    expect(JSON.stringify(log.mock.calls)).not.toContain('print(42)')
  })

  it('records a configuration failure without leaking its original error', async () => {
    const save = vi.fn(async () => undefined)
    const log = vi.fn()

    await expect(runPythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' } as Env,
      'user-1',
      'print(42)',
      {
        createStore: () => testStore(save),
        executeSandbox: async () => { throw new Error('Sandbox bridge configuration is incomplete') },
        log,
      },
    )).rejects.toMatchObject({
      message: 'Sandbox configuration is incomplete',
      status: 503,
      record: expect.objectContaining({ status: 'failed' }),
    } satisfies Partial<AgentRunServiceError>)

    expect(save).toHaveBeenCalledTimes(2)
    expect(save.mock.calls[1]).toEqual(['user-1', expect.objectContaining({ error: 'Sandbox configuration is incomplete' })])
    expect(log).toHaveBeenLastCalledWith('failed', expect.objectContaining({
      errorCode: 'bridge_configuration_incomplete',
      status: 'failed',
    }))
  })

  it('preserves a storage failure after a successful sandbox execution', async () => {
    const save = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('redis unavailable'))

    await expect(runPythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      {
        createStore: () => testStore(save),
        executeSandbox: async () => ({
          exitCode: 0,
          stdout: '42\n',
          stderr: '',
          activeCpuUsageMs: 1,
          networkIngressBytes: 0,
          networkEgressBytes: 0,
        }),
      },
    )).rejects.toMatchObject({ message: 'Agent run storage is unavailable', status: 503 })
  })

  it('does not create storage records while the sandbox is disabled', async () => {
    const createStore = vi.fn(() => testStore())

    await expect(runPythonResearch(
      { AGENT_SANDBOX_ENABLED: 'false' },
      'user-1',
      'print(42)',
      { createStore },
    )).rejects.toMatchObject({ message: 'Agent sandbox is disabled', status: 503 })

    expect(createStore).not.toHaveBeenCalled()
  })

  it('does not forward an unsafe request ID into execution logs', async () => {
    const log = vi.fn()

    await runPythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      {
        createStore: () => testStore(),
        executeSandbox: async () => ({
          exitCode: 0,
          stdout: '42\n',
          stderr: '',
          activeCpuUsageMs: 1,
          networkIngressBytes: 0,
          networkEgressBytes: 0,
        }),
        log,
        requestId: 'request with script=print(secret)',
      },
    )

    expect(log).toHaveBeenNthCalledWith(1, 'started', expect.objectContaining({ requestId: undefined }))
  })
})
