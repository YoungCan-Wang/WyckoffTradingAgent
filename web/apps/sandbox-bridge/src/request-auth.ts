import { createHmac, timingSafeEqual } from 'node:crypto'
import type { IncomingHttpHeaders } from 'node:http'

const MAX_CLOCK_SKEW_MS = 5 * 60_000

export function hasValidBridgeSignature(
  headers: IncomingHttpHeaders,
  body: string,
  secret: string,
  now = Date.now(),
): boolean {
  const timestamp = headerValue(headers, 'x-wyckoff-timestamp')
  const signature = headerValue(headers, 'x-wyckoff-signature')
  if (!timestamp || !signature || !isFreshTimestamp(timestamp, now)) return false
  const expected = createHmac('sha256', secret).update(`${timestamp}.${body}`).digest()
  return equalSignatures(expected, signature)
}

function headerValue(headers: IncomingHttpHeaders, name: string): string | undefined {
  const value = headers[name]
  return typeof value === 'string' ? value : undefined
}

function isFreshTimestamp(value: string, now: number): boolean {
  if (!/^\d{13}$/.test(value)) return false
  return Math.abs(now - Number(value)) <= MAX_CLOCK_SKEW_MS
}

function equalSignatures(expected: Buffer, supplied: string): boolean {
  if (!/^[a-f0-9]{64}$/i.test(supplied)) return false
  return timingSafeEqual(expected, Buffer.from(supplied, 'hex'))
}
