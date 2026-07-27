import type { Env } from '../app'
import type { AgentRunRecord } from './agent-run-store'

export type AgentRunNotify = (env: Env, userId: string, record: AgentRunRecord) => Promise<void>

// Best-effort push: a failed delivery must never affect run state, because the
// frontend still recovers through its polling fallback.
export const notifyAgentRun: AgentRunNotify = async (env, userId, record) => {
  const namespace = env.AGENT_RUN_NOTIFIER
  if (!namespace) return
  try {
    const stub = namespace.get(namespace.idFromName(userId))
    await stub.fetch('https://agent-run-notifier/notify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(record),
    })
  } catch {
    // Swallowed by design; see above.
  }
}
