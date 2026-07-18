import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiUrl } from '../api-url'

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
})
