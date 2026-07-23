import { describe, expect, it } from 'vitest'
import { app } from './index'

describe('API middleware', () => {
  it('adds request and security headers', async () => {
    const response = await app.request('/api/health', {
      headers: {
        Origin: 'http://localhost:5173',
        'X-Request-Id': 'test-request-1',
      },
    })

    expect(response.status).toBe(200)
    expect(response.headers.get('X-Request-Id')).toBe('test-request-1')
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff')
    expect(response.headers.get('Access-Control-Allow-Origin')).toBe('http://localhost:5173')
  })

  it('rejects oversized API request bodies before routing', async () => {
    const response = await app.request('/api/unknown', {
      method: 'POST',
      body: 'x'.repeat(256 * 1024 + 1),
    })

    expect(response.status).toBe(413)
    expect(await response.json()).toMatchObject({ error: 'Request body is too large' })
    expect(response.headers.get('X-Request-Id')).toBeTruthy()
  })

  it('returns structured not-found responses', async () => {
    const response = await app.request('/api/unknown')

    expect(response.status).toBe(404)
    expect(await response.json()).toMatchObject({ error: 'Not Found' })
  })
})
