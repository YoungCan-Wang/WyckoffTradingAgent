export type SandboxExecutionContext = {
  requestId?: string
  runId: string
}

export type SandboxRunLogEvent = 'queued' | 'started' | 'retrying' | 'finished' | 'failed' | 'cancelled'

type SandboxRunLogFields = SandboxExecutionContext & {
  durationMs?: number
  attempts?: number
  errorCode?: 'bridge_configuration_incomplete' | 'queue_delivery_failed' | 'queue_unavailable' | 'retry_exhausted' | 'sandbox_disabled' | 'sandbox_execution_failed' | 'storage_unavailable'
  exitCode?: number
  status?: 'completed' | 'failed'
  usage?: {
    activeCpuUsageMs: number
    networkIngressBytes: number
    networkEgressBytes: number
  }
}

export type SandboxRunLogger = (event: SandboxRunLogEvent, fields: SandboxRunLogFields) => void

const CORRELATION_ID_PATTERN = /^[A-Za-z0-9._:-]+$/

export function safeRequestId(value: string | undefined): string | undefined {
  if (!value || value.length > 128 || !CORRELATION_ID_PATTERN.test(value)) return undefined
  return value
}

export const logSandboxRun: SandboxRunLogger = (event, fields) => {
  const payload = JSON.stringify({
    event: `sandbox_run.${event}`,
    timestamp: new Date().toISOString(),
    ...fields,
  })
  if (event === 'failed') {
    console.error(payload)
    return
  }
  console.info(payload)
}
