import { describe, expect, it } from 'vitest'
import app from './pages'

describe('Pages compatibility API', () => {
  it('does not expose Worker-only agent run routes', async () => {
    const response = await app.request('/api/agent-runs/example')

    expect(response.status).toBe(404)
  })

  it('keeps shared API middleware and health checks', async () => {
    const response = await app.request('/api/health')

    expect(response.status).toBe(200)
    expect(response.headers.get('X-Content-Type-Options')).toBe('nosniff')
  })
})
