import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../app'
import { type AgentRunMessage } from './agent-run'
import {
  AGENT_RUN_DEAD_LETTER_QUEUE_NAME,
  AGENT_RUN_QUEUE_NAME,
  handleAgentRunQueue,
} from './agent-run-queue'

function message(body: AgentRunMessage, attempts = 1) {
  return {
    id: 'message-1',
    timestamp: new Date(),
    body,
    attempts,
    ack: vi.fn(),
    retry: vi.fn(),
  }
}

function batch(queue: string, messages: unknown[]): MessageBatch<AgentRunMessage> {
  return {
    queue,
    messages,
    metadata: { metrics: { backlogCount: 0, backlogBytes: 0 } },
    ackAll: vi.fn(),
    retryAll: vi.fn(),
  } as unknown as MessageBatch<AgentRunMessage>
}

const body: AgentRunMessage = {
  kind: 'python_research',
  runId: 'run-1',
  userId: 'user-1',
  script: 'print(42)',
}

describe('Agent run queue consumer', () => {
  it('retries transient failures with bounded backoff', async () => {
    const queuedMessage = message(body)

    await handleAgentRunQueue(batch(AGENT_RUN_QUEUE_NAME, [queuedMessage]), {} as Env, {
      consume: async () => 'retry',
    })

    expect(queuedMessage.retry).toHaveBeenCalledWith({ delaySeconds: 10 })
    expect(queuedMessage.ack).not.toHaveBeenCalled()
  })

  it('acknowledges terminal runs after the service persists their state', async () => {
    const queuedMessage = message(body, 2)

    await handleAgentRunQueue(batch(AGENT_RUN_QUEUE_NAME, [queuedMessage]), {} as Env, {
      consume: async () => 'ack',
    })

    expect(queuedMessage.ack).toHaveBeenCalledOnce()
    expect(queuedMessage.retry).not.toHaveBeenCalled()
  })

  it('turns dead-letter messages into visible failed runs', async () => {
    const deadLetter = message(body, 4)
    const failDeadLetter = vi.fn(async () => undefined)

    await handleAgentRunQueue(batch(AGENT_RUN_DEAD_LETTER_QUEUE_NAME, [deadLetter]), {} as Env, { failDeadLetter })

    expect(failDeadLetter).toHaveBeenCalledWith(expect.anything(), body)
    expect(deadLetter.ack).toHaveBeenCalledOnce()
  })

  it('acks malformed messages without exposing their payload', async () => {
    const malformed = message({ ...body, kind: 'python_research' })
    malformed.body = { kind: 'unknown', script: 'print(secret)' } as unknown as AgentRunMessage

    await handleAgentRunQueue(batch(AGENT_RUN_QUEUE_NAME, [malformed]), {} as Env)

    expect(malformed.ack).toHaveBeenCalledOnce()
    expect(malformed.retry).not.toHaveBeenCalled()
  })
})
