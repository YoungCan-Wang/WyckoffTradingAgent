export type SandboxExecutionContext = {
  requestId?: string
  runId: string
}

export type SandboxRunLogEvent = 'started' | 'finished' | 'failed'

type SandboxRunLogFields = SandboxExecutionContext & {
  durationMs?: number
  errorCode?: 'bridge_configuration_incomplete' | 'sandbox_execution_failed' | 'storage_unavailable'
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
