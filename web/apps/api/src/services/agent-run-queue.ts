import type { Env } from '../app'
import {
  consumePythonResearch,
  failDeadLetterAgentRun,
  isAgentRunMessage,
  type AgentRunMessage,
  type AgentRunOutcome,
} from './agent-run'
import { logSandboxRun, safeRequestId, type SandboxRunLogger } from './sandbox-observability'

export const AGENT_RUN_QUEUE_NAME = 'wyckoff-agent-runs'
export const AGENT_RUN_DEAD_LETTER_QUEUE_NAME = 'wyckoff-agent-runs-dlq'

type QueueHandlerDependencies = {
  consume?: (env: Env, message: AgentRunMessage) => Promise<AgentRunOutcome>
  failDeadLetter?: (env: Env, message: AgentRunMessage) => Promise<void>
  log?: SandboxRunLogger
}

export async function handleAgentRunQueue(
  batch: MessageBatch<AgentRunMessage>,
  env: Env,
  dependencies: QueueHandlerDependencies = {},
): Promise<void> {
  for (const message of batch.messages) {
    if (!isAgentRunMessage(message.body)) {
      message.ack()
      continue
    }
    await handleMessage(batch.queue, message, env, dependencies)
  }
}

async function handleMessage(
  queue: string,
  message: Message<AgentRunMessage>,
  env: Env,
  dependencies: QueueHandlerDependencies,
): Promise<void> {
  try {
    if (queue === AGENT_RUN_DEAD_LETTER_QUEUE_NAME) {
      await (dependencies.failDeadLetter || failDeadLetterAgentRun)(env, message.body)
      message.ack()
      return
    }
    if (queue !== AGENT_RUN_QUEUE_NAME) {
      message.ack()
      return
    }
    const outcome = await (dependencies.consume || consumePythonResearch)(env, message.body)
    if (outcome === 'retry') {
      message.retry({ delaySeconds: retryDelay(message.attempts) })
      return
    }
    message.ack()
  } catch {
    const log = dependencies.log || logSandboxRun
    log('retrying', {
      requestId: safeRequestId(message.body.requestId),
      runId: message.body.runId,
      attempts: message.attempts,
      errorCode: 'storage_unavailable',
      status: 'failed',
    })
    message.retry({ delaySeconds: retryDelay(message.attempts) })
  }
}

function retryDelay(attempts: number): number {
  return Math.min(10 * 2 ** Math.max(attempts - 1, 0), 60)
}
