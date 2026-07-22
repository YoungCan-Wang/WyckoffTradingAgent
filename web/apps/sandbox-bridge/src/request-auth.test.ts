import { createHmac } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { hasValidBridgeSignature } from './request-auth.js'

const body = '{"script":"print(1)","timeout":60000}'
const secret = 'test-bridge-secret'
const timestamp = '1784678400000'

function headers(signature: string) {
  return {
    'x-wyckoff-timestamp': timestamp,
    'x-wyckoff-signature': signature,
  }
}

function signature() {
  return createHmac('sha256', secret).update(`${timestamp}.${body}`).digest('hex')
}

describe('sandbox bridge request authentication', () => {
  it('accepts a current signed request', () => {
    expect(hasValidBridgeSignature(headers(signature()), body, secret, Number(timestamp))).toBe(true)
  })

  it('rejects a modified body and stale timestamp', () => {
    expect(hasValidBridgeSignature(headers(signature()), `${body} `, secret, Number(timestamp))).toBe(false)
    expect(hasValidBridgeSignature(headers(signature()), body, secret, Number(timestamp) + 300_001)).toBe(false)
  })
})
