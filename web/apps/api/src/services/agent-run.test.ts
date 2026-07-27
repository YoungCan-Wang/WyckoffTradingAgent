import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import type { AgentRunRecord, AgentRunStore } from './agent-run-store'
import {
  AgentRunServiceError,
  consumePythonResearch,
  enqueuePythonResearch,
  failDeadLetterAgentRun,
  type AgentRunMessage,
} from './agent-run'

const queuedRecord: AgentRunRecord = {
  id: 'run-1',
  kind: 'python_research',
  status: 'queued',
  attempts: 0,
  createdAt: '2026-07-23T00:00:00.000Z',
}

const runningRecord: AgentRunRecord = { ...queuedRecord, status: 'running', attempts: 1 }

function testStore(overrides: Partial<AgentRunStore> = {}): AgentRunStore {
  return {
    save: vi.fn(async () => undefined),
    get: vi.fn(async () => null),
    remove: vi.fn(async () => undefined),
    cancel: vi.fn(async () => null),
    claim: vi.fn(async () => runningRecord),
    requeue: vi.fn(async () => ({ ...runningRecord, status: 'queued' as const })),
    fail: vi.fn(async (_userId, record, error) => ({ ...record, status: 'failed' as const, error })),
    acquireLease: vi.fn(async () => true),
    releaseLease: vi.fn(async () => undefined),
    acquireActiveSlot: vi.fn(async () => true),
    releaseActiveSlot: vi.fn(async () => undefined),
    dailyCpuUsage: vi.fn(async () => 0),
    addDailyCpuUsage: vi.fn(async () => 17),
    ...overrides,
  } as AgentRunStore
}

function runMessage(overrides: Partial<AgentRunMessage> = {}): AgentRunMessage {
  return { kind: 'python_research', runId: 'run-1', userId: 'user-1', script: 'print(42)', ...overrides }
}

const successfulResult = {
  exitCode: 0,
  stdout: '42\n',
  stderr: '',
  activeCpuUsageMs: 17,
  networkIngressBytes: 0,
  networkEgressBytes: 0,
}

describe('Agent run service', () => {
  it('persists a queued record and sends one queue message without executing the script', async () => {
    const store = testStore()
    const queue = { send: vi.fn(async () => ({ metadata: {} })) }
    const executeSandbox = vi.fn(async () => successfulResult)
    const log = vi.fn()

    const record = await enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue, executeSandbox, log, requestId: 'request-1' },
    )

    expect(record).toMatchObject({ kind: 'python_research', status: 'queued', attempts: 0 })
    expect(store.save).toHaveBeenCalledWith('user-1', expect.objectContaining({ status: 'queued' }))
    expect(queue.send).toHaveBeenCalledWith(expect.objectContaining({
      kind: 'python_research',
      userId: 'user-1',
      script: 'print(42)',
      requestId: 'request-1',
    }))
    expect(executeSandbox).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledWith('queued', expect.objectContaining({ requestId: 'request-1' }))
    expect(JSON.stringify(log.mock.calls)).not.toContain('print(42)')
  })

  it('rejects a run before persisting anything when the sandbox quota is exhausted', async () => {
    const store = testStore()
    const queue = { send: vi.fn(async () => ({})) }

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      {
        createStore: () => store,
        queue,
        checkQuota: async () => ({ ok: false, mode: 'redis', limit: 20, remaining: 0, reset: 0, message: '今日沙箱任务额度已用完，请明天再试。' }),
      },
    )).rejects.toMatchObject({ status: 429, message: '今日沙箱任务额度已用完，请明天再试。' })

    expect(store.save).not.toHaveBeenCalled()
    expect(queue.send).not.toHaveBeenCalled()
    expect(store.acquireActiveSlot).toHaveBeenCalledWith('user-1', expect.any(String))
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', expect.any(String))
  })

  it('fails closed when the sandbox quota service is unavailable', async () => {
    const store = testStore()
    const queue = { send: vi.fn(async () => ({})) }

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue, checkQuota: async () => { throw new Error('offline') } },
    )).rejects.toMatchObject({ status: 503, message: '沙箱额度服务暂不可用，请稍后再试。' })

    expect(store.save).not.toHaveBeenCalled()
    expect(queue.send).not.toHaveBeenCalled()
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', expect.any(String))
  })

  it('rejects a second queued or running sandbox task for the same user', async () => {
    const store = testStore({ acquireActiveSlot: vi.fn(async () => false) })
    const checkQuota = vi.fn(async () => ({ ok: true, mode: 'redis' as const, limit: 20, remaining: 19, reset: 0 }))

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue: { send: vi.fn(async () => ({})) }, checkQuota },
    )).rejects.toMatchObject({
      status: 429,
      message: '当前已有一个沙箱任务正在排队或执行，请等待它结束后再提交。',
    })

    expect(checkQuota).not.toHaveBeenCalled()
    expect(store.save).not.toHaveBeenCalled()
    expect(store.releaseActiveSlot).not.toHaveBeenCalled()
  })

  it('rejects a run when the daily CPU budget is exhausted', async () => {
    const store = testStore({ dailyCpuUsage: vi.fn(async () => 120_000) })

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true', AGENT_RUN_DAILY_CPU_LIMIT_MS: '120000' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue: { send: vi.fn(async () => ({})) } },
    )).rejects.toMatchObject({ status: 429, message: '今日沙箱 CPU 额度已用完，请明天再试。' })

    expect(store.acquireActiveSlot).not.toHaveBeenCalled()
  })

  it('fails closed when the daily CPU budget cannot be read', async () => {
    const store = testStore({ dailyCpuUsage: vi.fn(async () => { throw new Error('offline') }) })

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue: { send: vi.fn(async () => ({})) } },
    )).rejects.toMatchObject({ status: 503, message: '沙箱 CPU 额度服务暂不可用，请稍后再试。' })

    expect(store.acquireActiveSlot).not.toHaveBeenCalled()
  })

  it('marks a run failed when Queue delivery cannot be confirmed', async () => {
    const fail = vi.fn(async (_userId, record: AgentRunRecord, error: string) => ({
      ...record,
      status: 'failed' as const,
      error,
    }))
    const store = testStore({ fail })

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => store, queue: { send: async () => { throw new Error('unavailable') } } },
    )).rejects.toMatchObject({
      message: 'Agent run queue is unavailable',
      status: 503,
      record: expect.objectContaining({ status: 'failed' }),
    } satisfies Partial<AgentRunServiceError>)

    expect(fail).toHaveBeenCalledWith('user-1', expect.objectContaining({ status: 'queued' }), 'Agent run queue is unavailable')
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', expect.any(String))
  })

  it('executes a claimed message once and stores its completed result', async () => {
    const save = vi.fn(async () => undefined)
    const log = vi.fn()
    const store = testStore({ save })
    const executeSandbox = vi.fn(async () => successfulResult)

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage({ requestId: 'request-1' }),
      { createStore: () => store, executeSandbox, log },
    )).resolves.toBe('ack')

    expect(executeSandbox).toHaveBeenCalledWith(
      expect.anything(),
      'print(42)',
      expect.objectContaining({ requestId: 'request-1', runId: 'run-1' }),
    )
    expect(save).toHaveBeenCalledWith('user-1', expect.objectContaining({ status: 'completed', stdout: '42\n' }))
    expect(store.releaseLease).toHaveBeenCalledWith('user-1', 'run-1')
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', 'run-1')
    expect(store.addDailyCpuUsage).toHaveBeenCalledWith('user-1', 17)
    expect(log).toHaveBeenNthCalledWith(1, 'started', expect.objectContaining({ attempts: 1 }))
    expect(log).toHaveBeenNthCalledWith(2, 'finished', expect.objectContaining({
      status: 'completed',
      usage: { activeCpuUsageMs: 17, networkIngressBytes: 0, networkEgressBytes: 0 },
    }))
  })

  it('stores a non-zero script result as a terminal failure without retrying it', async () => {
    const save = vi.fn(async () => undefined)
    const store = testStore({ save })

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage(),
      { createStore: () => store, executeSandbox: async () => ({ ...successfulResult, exitCode: 1, stderr: 'bad input' }) },
    )).resolves.toBe('ack')

    expect(save).toHaveBeenCalledWith('user-1', expect.objectContaining({ status: 'failed', exitCode: 1 }))
    expect(store.requeue).not.toHaveBeenCalled()
  })

  it('does not retry a completed execution when CPU metering is unavailable', async () => {
    const log = vi.fn()
    const store = testStore({ addDailyCpuUsage: vi.fn(async () => { throw new Error('offline') }) })

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage(),
      { createStore: () => store, executeSandbox: async () => successfulResult, log },
    )).resolves.toBe('ack')

    expect(store.requeue).not.toHaveBeenCalled()
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', 'run-1')
    expect(log).toHaveBeenCalledWith('metering_failed', expect.objectContaining({ status: 'completed' }))
  })

  it('does not retry a completed execution when its result cannot be persisted', async () => {
    const store = testStore({ save: vi.fn(async () => { throw new Error('offline') }) })

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage(),
      { createStore: () => store, executeSandbox: async () => successfulResult },
    )).resolves.toBe('ack')

    expect(store.requeue).not.toHaveBeenCalled()
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', 'run-1')
  })

  it('requeues a transient bridge failure while preserving the attempt count', async () => {
    const requeue = vi.fn(async () => ({ ...runningRecord, status: 'queued' as const }))
    const log = vi.fn()
    const store = testStore({ requeue })

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage(),
      { createStore: () => store, executeSandbox: async () => { throw new Error('bridge unavailable') }, log },
    )).resolves.toBe('retry')

    expect(requeue).toHaveBeenCalledWith('user-1', runningRecord, 'Sandbox execution failed')
    expect(log).toHaveBeenLastCalledWith('retrying', expect.objectContaining({
      attempts: 1,
      errorCode: 'sandbox_execution_failed',
    }))
  })

  it('records a configuration failure without retrying or leaking its original error', async () => {
    const fail = vi.fn(async (_userId, record: AgentRunRecord, error: string) => ({
      ...record,
      status: 'failed' as const,
      error,
    }))
    const log = vi.fn()
    const store = testStore({ fail })

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' } as Env,
      runMessage(),
      {
        createStore: () => store,
        executeSandbox: async () => { throw new Error('Sandbox bridge configuration is incomplete') },
        log,
      },
    )).resolves.toBe('ack')

    expect(fail).toHaveBeenCalledWith('user-1', runningRecord, 'Sandbox configuration is incomplete')
    expect(log).toHaveBeenLastCalledWith('failed', expect.objectContaining({
      errorCode: 'bridge_configuration_incomplete',
      status: 'failed',
    }))
  })

  it('pushes running and terminal status updates while consuming a message', async () => {
    const notify = vi.fn(async () => undefined)

    await expect(consumePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      runMessage(),
      { createStore: () => testStore(), executeSandbox: async () => successfulResult, notify },
    )).resolves.toBe('ack')

    expect(notify).toHaveBeenNthCalledWith(1, expect.anything(), 'user-1', expect.objectContaining({ status: 'running' }))
    expect(notify).toHaveBeenNthCalledWith(2, expect.anything(), 'user-1', expect.objectContaining({ status: 'completed' }))
  })

  it('marks a dead-lettered run as a visible terminal failure', async () => {
    const fail = vi.fn(async (_userId, record: AgentRunRecord, error: string) => ({
      ...record,
      status: 'failed' as const,
      error,
    }))
    const log = vi.fn()
    const notify = vi.fn(async () => undefined)
    const store = testStore({ get: vi.fn(async () => queuedRecord), fail })

    await failDeadLetterAgentRun({} as Env, runMessage(), { createStore: () => store, log, notify })

    expect(fail).toHaveBeenCalledWith('user-1', queuedRecord, 'Agent run exhausted retries')
    expect(store.releaseActiveSlot).toHaveBeenCalledWith('user-1', 'run-1')
    expect(notify).toHaveBeenCalledWith(expect.anything(), 'user-1', expect.objectContaining({ status: 'failed' }))
    expect(log).toHaveBeenCalledWith('failed', expect.objectContaining({
      errorCode: 'retry_exhausted',
      status: 'failed',
    }))
  })

  it('does not create a record while the sandbox is disabled', async () => {
    const createStore = vi.fn(() => testStore())

    await expect(enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'false' },
      'user-1',
      'print(42)',
      { createStore },
    )).rejects.toMatchObject({ message: 'Agent sandbox is disabled', status: 503 })

    expect(createStore).not.toHaveBeenCalled()
  })

  it('does not forward an unsafe request ID into Queue payloads or logs', async () => {
    const queue = { send: vi.fn(async () => ({ metadata: {} })) }
    const log = vi.fn()

    await enqueuePythonResearch(
      { AGENT_SANDBOX_ENABLED: 'true' },
      'user-1',
      'print(42)',
      { createStore: () => testStore(), queue, log, requestId: 'request with script=print(secret)' },
    )

    expect(queue.send).toHaveBeenCalledWith(expect.objectContaining({ requestId: undefined }))
    expect(log).toHaveBeenCalledWith('queued', expect.objectContaining({ requestId: undefined }))
  })
})
