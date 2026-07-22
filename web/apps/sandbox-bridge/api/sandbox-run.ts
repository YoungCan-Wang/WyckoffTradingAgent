import type { IncomingMessage, ServerResponse } from 'node:http'
import { hasValidBridgeSignature } from '../src/request-auth.js'
import { executePythonSandbox } from '../src/sandbox.js'

const MAX_REQUEST_BYTES = 16 * 1024
const DEFAULT_TIMEOUT_MS = 60_000
const MAX_TIMEOUT_MS = 120_000

export const config = { api: { bodyParser: false } }

export default async function handler(request: IncomingMessage, response: ServerResponse): Promise<void> {
  if (request.method !== 'POST') return reply(response, 405, { error: 'Method not allowed' })
  const body = await readBody(request)
  const secret = process.env.SANDBOX_BRIDGE_SECRET?.trim()
  if (!body || !secret || !hasValidBridgeSignature(request.headers, body, secret)) {
    return reply(response, 401, { error: 'Unauthorized' })
  }
  const payload = parsePayload(body)
  if (!payload) return reply(response, 400, { error: 'Invalid sandbox request' })

  try {
    return reply(response, 200, await executePythonSandbox(payload.script, payload.timeout))
  } catch {
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
