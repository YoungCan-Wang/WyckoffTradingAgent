import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiUrl, apiWsUrl, toWebSocketUrl } from '../api-url'

afterEach(() => vi.unstubAllEnvs())

describe('apiUrl', () => {
  it('uses the configured backend without a duplicate slash', () => {
    vi.stubEnv('VITE_API_URL', 'https://api.example.com/')

    expect(apiUrl('/api/chat')).toBe('https://api.example.com/api/chat')
  })

  it('uses the local Worker during development', () => {
    vi.stubEnv('VITE_API_URL', '')

    expect(apiUrl('/api/portfolio')).toBe('http://127.0.0.1:8787/api/portfolio')
  })

  it('uses a same-origin relative path in production', () => {
    expect(apiUrl('/api/chat', { DEV: false })).toBe('/api/chat')
    expect(apiUrl('/api/settings/test-model', { DEV: false })).toBe('/api/settings/test-model')
    expect(apiUrl('/api/agent-runs/ws', { DEV: false })).toBe('/api/agent-runs/ws')
  })

  it('still honors an explicit debug override in production', () => {
    expect(apiUrl('/api/remote/ws', {
      DEV: false,
      VITE_API_URL: 'https://wyckoff-api.yongkai-wang.workers.dev',
    })).toBe('https://wyckoff-api.yongkai-wang.workers.dev/api/remote/ws')
  })
})

describe('apiWsUrl', () => {
  it('rewrites the local Worker HTTP URL for development sockets', () => {
    expect(apiWsUrl('/api/agent-runs/ws', { DEV: true })).toBe('ws://127.0.0.1:8787/api/agent-runs/ws')
  })

  it('keeps a production relative path unless a page origin is available', () => {
    expect(toWebSocketUrl('/api/agent-runs/ws')).toBe('/api/agent-runs/ws')
    expect(toWebSocketUrl('/api/remote/ws?role=remote', 'https://wyckoff-analysis.pages.dev'))
      .toBe('wss://wyckoff-analysis.pages.dev/api/remote/ws?role=remote')
  })
})
