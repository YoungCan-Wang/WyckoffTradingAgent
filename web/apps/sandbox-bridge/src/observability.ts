import type { IncomingHttpHeaders } from 'node:http'

type BridgeLogEvent = 'started' | 'finished' | 'rejected' | 'failed'

type BridgeLogFields = {
  durationMs?: number
  errorCode?: 'invalid_request' | 'sandbox_execution_failed' | 'unauthorized'
  exitCode?: number
  requestId?: string
  runId?: string
  scriptBytes?: number
  status?: 'completed' | 'failed'
  timeoutMs?: number
  usage?: {
    activeCpuUsageMs: number
    networkIngressBytes: number
    networkEgressBytes: number
  }
}

const CORRELATION_ID_PATTERN = /^[A-Za-z0-9._:-]+$/

export function bridgeLogContext(headers: IncomingHttpHeaders): Pick<BridgeLogFields, 'requestId' | 'runId'> {
  return {
    requestId: validHeader(headers['x-wyckoff-request-id'], 128),
    runId: validHeader(headers['x-wyckoff-run-id'], 64),
  }
}

export function logSandboxBridge(event: BridgeLogEvent, fields: BridgeLogFields): void {
  const payload = JSON.stringify({
    event: `sandbox_bridge.${event}`,
    timestamp: new Date().toISOString(),
    ...fields,
  })
  if (event === 'failed' || event === 'rejected') {
    console.error(payload)
    return
  }
  console.info(payload)
}

function validHeader(value: string | string[] | undefined, maxLength: number): string | undefined {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength) return undefined
  return CORRELATION_ID_PATTERN.test(value) ? value : undefined
}
