import type { IncomingMessage, ServerResponse } from 'node:http'
import { bridgeLogContext, logSandboxBridge } from '../src/observability.js'
import { hasValidBridgeSignature } from '../src/request-auth.js'
import { executePythonSandbox } from '../src/sandbox.js'

const MAX_REQUEST_BYTES = 16 * 1024
const DEFAULT_TIMEOUT_MS = 60_000
const MAX_TIMEOUT_MS = 120_000

export const config = { api: { bodyParser: false } }

export default async function handler(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const startedAt = Date.now()
  const context = bridgeLogContext(request.headers)
  if (request.method !== 'POST') return reply(response, 405, { error: 'Method not allowed' })
  const body = await readBody(request)
  const secret = process.env.SANDBOX_BRIDGE_SECRET?.trim()
  if (!body || !secret || !hasValidBridgeSignature(request.headers, body, secret)) {
    logSandboxBridge('rejected', { durationMs: Date.now() - startedAt, errorCode: 'unauthorized' })
    return reply(response, 401, { error: 'Unauthorized' })
  }
  const payload = parsePayload(body)
  if (!payload) {
    logSandboxBridge('rejected', { ...context, durationMs: Date.now() - startedAt, errorCode: 'invalid_request' })
    return reply(response, 400, { error: 'Invalid sandbox request' })
  }
  logSandboxBridge('started', {
    ...context,
    scriptBytes: Buffer.byteLength(payload.script),
    timeoutMs: payload.timeout,
  })

  try {
    const result = await executePythonSandbox(payload.script, payload.timeout)
    logSandboxBridge('finished', {
      ...context,
      durationMs: Date.now() - startedAt,
      exitCode: result.exitCode,
      status: result.exitCode === 0 ? 'completed' : 'failed',
      usage: {
        activeCpuUsageMs: result.activeCpuUsageMs,
        networkIngressBytes: result.networkIngressBytes,
        networkEgressBytes: result.networkEgressBytes,
      },
    })
    return reply(response, 200, result)
  } catch {
    logSandboxBridge('failed', {
      ...context,
      durationMs: Date.now() - startedAt,
      errorCode: 'sandbox_execution_failed',
    })
    return reply(response, 502, { error: 'Sandbox execution failed' })
  }
}

async function readBody(request: IncomingMessage): Promise<string | null> {
  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += buffer.length
    if (size > MAX_REQUEST_BYTES) return null
    chunks.push(buffer)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function parsePayload(body: string): { script: string; timeout: number } | null {
  try {
    const value = JSON.parse(body) as { script?: unknown; timeout?: unknown }
    if (typeof value.script !== 'string' || !value.script.trim() || value.script.length > 12_000) return null
    return { script: value.script, timeout: normalizeTimeout(value.timeout) }
  } catch {
    return null
  }
}

function normalizeTimeout(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 5_000) return DEFAULT_TIMEOUT_MS
  return Math.min(Math.trunc(value), MAX_TIMEOUT_MS)
}

function reply(response: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body)
  response.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) })
  response.end(payload)
}
