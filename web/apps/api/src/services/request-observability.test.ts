import { describe, expect, it, vi } from 'vitest'
import { logUnhandledWorkerError, readOptionalUserId, sanitizeLogText } from './request-observability'

describe('request observability', () => {
  it('redacts bearer tokens and truncates log text', () => {
    expect(sanitizeLogText('Bearer abcdefghijklmnop failed')).toContain('[redacted]')
    expect(sanitizeLogText('x'.repeat(240)).length).toBe(200)
  })

  it('reads an authenticated user id when the request already has auth', () => {
    expect(readOptionalUserId({ get: () => ({ userId: 'user-1' }) })).toBe('user-1')
    expect(readOptionalUserId({ get: () => undefined })).toBeUndefined()
  })

  it('writes a structured worker_error without leaking secrets', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    logUnhandledWorkerError(new Error('Bearer super-secret-token exploded'), {
      get: (key) => (key === 'auth' ? { userId: 'user-1' } : 'req-1'),
      req: { method: 'POST', path: '/api/chat' },
    })
    const payload = JSON.parse(String(error.mock.calls[0]?.[0])) as { event: string; message: string; userId: string }
    expect(payload).toMatchObject({ event: 'worker_error', userId: 'user-1' })
    expect(payload.message).toContain('[redacted]')
    expect(payload.message).not.toContain('super-secret-token')
    error.mockRestore()
  })
})
