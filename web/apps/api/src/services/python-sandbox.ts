import type { Env } from '../app'
import type { SandboxExecutionContext } from './sandbox-observability'

const DEFAULT_TIMEOUT_MS = 60_000
const MAX_TIMEOUT_MS = 120_000

export type PythonSandboxResult = {
  exitCode: number
  stdout: string
  stderr: string
  activeCpuUsageMs: number
  networkIngressBytes: number
  networkEgressBytes: number
}

export type SandboxBridgeFetch = (input: string, init: RequestInit) => Promise<Response>

export async function executePythonSandbox(
  env: Env,
  script: string,
  context: SandboxExecutionContext = { runId: crypto.randomUUID() },
  bridgeFetch: SandboxBridgeFetch = fetch,
): Promise<PythonSandboxResult> {
  const bridge = bridgeConfig(env)
  const body = JSON.stringify({ script, timeout: sandboxTimeout(env.AGENT_SANDBOX_TIMEOUT_MS) })
  const timestamp = String(Date.now())
  const response = await bridgeFetch(bridge.url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Wyckoff-Timestamp': timestamp,
      'X-Wyckoff-Signature': await bridgeSignature(bridge.secret, timestamp, body),
      ...correlationHeaders(context),
    },
    body,
  })
  if (!response.ok) throw new Error('Sandbox bridge request failed')
  return parseResult(await response.json())
}

function correlationHeaders(context: SandboxExecutionContext): Record<string, string> {
  const headers: Record<string, string> = { 'X-Wyckoff-Run-Id': context.runId }
  if (context.requestId) headers['X-Wyckoff-Request-Id'] = context.requestId
  return headers
}

function bridgeConfig(env: Env): { url: string; secret: string } {
  const url = env.SANDBOX_BRIDGE_URL?.trim()
  const secret = env.SANDBOX_BRIDGE_SECRET?.trim()
  if (!url || !secret || !isHttpsUrl(url)) throw new Error('Sandbox bridge configuration is incomplete')
  return { url, secret }
}

function isHttpsUrl(value: string): boolean {
  try {
    return new URL(value).protocol === 'https:'
  } catch {
    return false
  }
}

async function bridgeSignature(secret: string, timestamp: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  const signature = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`${timestamp}.${body}`))
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
}

function sandboxTimeout(raw: string | undefined): number {
  const parsed = Number(raw)
  if (!Number.isFinite(parsed) || parsed < 5_000) return DEFAULT_TIMEOUT_MS
  return Math.min(Math.trunc(parsed), MAX_TIMEOUT_MS)
}

function parseResult(value: unknown): PythonSandboxResult {
  if (!isSandboxResult(value)) throw new Error('Sandbox bridge returned an invalid result')
  return value
}

function isSandboxResult(value: unknown): value is PythonSandboxResult {
  if (!value || typeof value !== 'object') return false
  const result = value as Record<string, unknown>
  return typeof result.exitCode === 'number'
    && typeof result.stdout === 'string'
    && typeof result.stderr === 'string'
    && typeof result.activeCpuUsageMs === 'number'
    && typeof result.networkIngressBytes === 'number'
    && typeof result.networkEgressBytes === 'number'
}
