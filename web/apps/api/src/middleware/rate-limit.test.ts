import { Hono } from 'hono'
import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../index'
import type { AuthContext } from './auth'
import { createChatRateLimitMiddleware, type ChatRateLimitResult } from './rate-limit'

type TestBindings = {
  Bindings: Env
  Variables: { auth: AuthContext }
}

function testApp(
  middleware: ReturnType<typeof createChatRateLimitMiddleware>,
  userId = 'user-1',
) {
  const app = new Hono<TestBindings>()
  app.use('*', async (c, next) => {
    c.set('auth', { userId, accessToken: 'token' })
    await next()
  })
  app.post('/', middleware, (c) => c.json({ ok: true }))
  return app
}

describe('chat rate limit middleware', () => {
  it('uses configured local limits when Redis is absent', async () => {
    let now = Date.UTC(2026, 6, 18, 0, 0, 0)
    const app = testApp(createChatRateLimitMiddleware({ now: () => now }))
    const env = { CHAT_DAILY_LIMIT_PER_USER: '2', CHAT_MIN_INTERVAL_MS: '1000' }

    const first = await app.request('/', { method: 'POST' }, env)
    const tooSoon = await app.request('/', { method: 'POST' }, env)
    now += 1000
    const second = await app.request('/', { method: 'POST' }, env)
    now += 1000
    const exhausted = await app.request('/', { method: 'POST' }, env)

    expect(first.status).toBe(200)
    expect(first.headers.get('X-RateLimit-Backend')).toBe('local')
    expect(tooSoon.status).toBe(429)
    expect(await tooSoon.json()).toEqual({ error: '请求太频繁，请稍后再试。' })
    expect(second.status).toBe(200)
    expect(exhausted.status).toBe(429)
    expect(await exhausted.json()).toEqual({ error: '今日读盘室免费额度已用完，请明天再试。' })
  })

  it('uses the distributed result and exposes quota headers', async () => {
    const result: Omit<ChatRateLimitResult, 'mode'> = {
      ok: true,
      limit: 80,
      remaining: 79,
      reset: Date.UTC(2026, 6, 19),
    }
    const check = vi.fn(async () => result)
    const app = testApp(createChatRateLimitMiddleware({
      createDistributedLimiter: () => ({ check }),
    }))

    const response = await app.request('/', { method: 'POST' }, {})

    expect(response.status).toBe(200)
    expect(check).toHaveBeenCalledWith('user-1')
    expect(response.headers.get('X-RateLimit-Backend')).toBe('redis')
    expect(response.headers.get('X-RateLimit-Remaining')).toBe('79')
  })

  it('falls back to local protection when Redis is unavailable', async () => {
    const app = testApp(createChatRateLimitMiddleware({
      createDistributedLimiter: () => ({ check: async () => { throw new Error('offline') } }),
    }))

    const response = await app.request('/', { method: 'POST' }, {})

    expect(response.status).toBe(200)
    expect(response.headers.get('X-RateLimit-Backend')).toBe('local-fallback')
  })
})

const runtimeEnv = (globalThis as typeof globalThis & {
  process?: { env: Record<string, string | undefined> }
}).process?.env
const describeUpstash = runtimeEnv?.RUN_UPSTASH_INTEGRATION === '1' ? describe : describe.skip

describeUpstash('Upstash Redis integration', () => {
  it('enforces the real distributed interval limit', async () => {
    const url = runtimeEnv?.UPSTASH_REDIS_REST_URL
    const token = runtimeEnv?.UPSTASH_REDIS_REST_TOKEN
    expect(url).toBeTruthy()
    expect(token).toBeTruthy()

    const app = testApp(createChatRateLimitMiddleware(), `live-${crypto.randomUUID()}`)
    const env: Env = {
      UPSTASH_REDIS_REST_URL: url,
      UPSTASH_REDIS_REST_TOKEN: token,
      CHAT_DAILY_LIMIT_PER_USER: '2',
      CHAT_MIN_INTERVAL_MS: '5000',
    }

    const first = await app.request('/', { method: 'POST' }, env)
    const tooSoon = await app.request('/', { method: 'POST' }, env)

    expect(first.status).toBe(200)
    expect(first.headers.get('X-RateLimit-Backend')).toBe('redis')
    expect(tooSoon.status).toBe(429)
    expect(tooSoon.headers.get('X-RateLimit-Backend')).toBe('redis')
    expect(await tooSoon.json()).toEqual({ error: '请求太频繁，请稍后再试。' })
  })
})
