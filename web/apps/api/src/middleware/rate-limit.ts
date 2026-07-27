import { Ratelimit, type Duration } from '@upstash/ratelimit'
import { Redis } from '@upstash/redis/cloudflare'
import { createMiddleware } from 'hono/factory'
import type { Env } from '../app'
import type { AuthContext } from './auth'

type RateLimitBindings = {
  Bindings: Env
  Variables: { auth: AuthContext }
}

type RateLimitMode = 'local' | 'redis' | 'local-fallback'

export type RateLimitResult = {
  ok: boolean
  mode: RateLimitMode
  limit: number
  remaining: number
  reset: number
  message?: string
}

export type RateLimitPolicy = {
  prefix: string
  dailyLimit: (env: Env) => number
  minIntervalMs: (env: Env) => number
  intervalMessage: string
  dailyMessage: string
}

export const CHAT_RATE_LIMIT_POLICY: RateLimitPolicy = {
  prefix: 'chat',
  dailyLimit: (env) => positiveInt(env.CHAT_DAILY_LIMIT_PER_USER, 80),
  minIntervalMs: (env) => positiveInt(env.CHAT_MIN_INTERVAL_MS, 2500),
  intervalMessage: '请求太频繁，请稍后再试。',
  dailyMessage: '今日读盘室免费额度已用完，请明天再试。',
}

export const AGENT_RUN_RATE_LIMIT_POLICY: RateLimitPolicy = {
  prefix: 'agent-run',
  dailyLimit: (env) => positiveInt(env.AGENT_RUN_DAILY_LIMIT_PER_USER, 20),
  minIntervalMs: (env) => positiveInt(env.AGENT_RUN_MIN_INTERVAL_MS, 10_000),
  intervalMessage: '沙箱任务提交太频繁，请稍后再试。',
  dailyMessage: '今日沙箱任务额度已用完，请明天再试。',
}

type DistributedLimiter = {
  check: (userId: string) => Promise<Omit<RateLimitResult, 'mode'>>
}

type LocalLimiter = {
  check: (env: Env, userId: string) => Promise<Omit<RateLimitResult, 'mode'>>
}

type RateLimitOptions = {
  now?: () => number
  createDistributedLimiter?: (env: Env) => DistributedLimiter | null
}

type LocalState = { day: string; count: number; lastAt: number }

export function createRateLimitMiddleware(policy: RateLimitPolicy, options: RateLimitOptions = {}) {
  const now = options.now || Date.now
  const localLimiter = createLocalLimiter(now, policy)
  const distributedFactory = options.createDistributedLimiter || ((env: Env) => createUpstashLimiter(env, policy))

  return createMiddleware<RateLimitBindings>(async (c, next) => {
    const userId = c.get('auth').userId
    const result = await checkRateLimit(c.env, userId, localLimiter, distributedFactory)
    setRateLimitHeaders((name, value) => c.header(name, value), result)
    if (!result.ok) return c.json({ error: result.message }, 429)
    await next()
  })
}

export const chatRateLimitMiddleware = createRateLimitMiddleware(CHAT_RATE_LIMIT_POLICY)

// Sandbox creation quota is enforced at the enqueue boundary (not as HTTP middleware)
// so chat-tool and REST submissions consume one shared budget. A missing Redis
// configuration returns null because the run store will reject the same request;
// a configured limiter that fails must propagate so creation fails closed.
export async function checkAgentRunQuota(env: Env, userId: string): Promise<RateLimitResult | null> {
  const limiter = createUpstashLimiter(env, AGENT_RUN_RATE_LIMIT_POLICY)
  if (!limiter) return null
  return { ...(await limiter.check(userId)), mode: 'redis' }
}

async function checkRateLimit(
  env: Env,
  userId: string,
  localLimiter: LocalLimiter,
  distributedFactory: (env: Env) => DistributedLimiter | null,
): Promise<RateLimitResult> {
  const distributed = distributedFactory(env)
  if (!distributed) return { ...(await localLimiter.check(env, userId)), mode: 'local' }
  try {
    return { ...(await distributed.check(userId)), mode: 'redis' }
  } catch {
    return { ...(await localLimiter.check(env, userId)), mode: 'local-fallback' }
  }
}

function createLocalLimiter(now: () => number, policy: RateLimitPolicy): LocalLimiter {
  const states = new Map<string, LocalState>()
  return {
    check: async (env, userId) => {
      const timestamp = now()
      const day = new Date(timestamp).toISOString().slice(0, 10)
      const state = states.get(userId)
      const current = state?.day === day ? state : { day, count: 0, lastAt: 0 }
      const limit = policy.dailyLimit(env)
      const minInterval = policy.minIntervalMs(env)
      if (timestamp - current.lastAt < minInterval) {
        return denied(limit, current.count, current.lastAt + minInterval, policy.intervalMessage)
      }
      if (current.count >= limit) {
        return denied(limit, current.count, nextUtcDay(timestamp), policy.dailyMessage)
      }
      states.set(userId, { day, count: current.count + 1, lastAt: timestamp })
      return allowed(limit, current.count + 1, nextUtcDay(timestamp))
    },
  }
}

function createUpstashLimiter(env: Env, policy: RateLimitPolicy): DistributedLimiter | null {
  const url = env.UPSTASH_REDIS_REST_URL?.trim()
  const token = env.UPSTASH_REDIS_REST_TOKEN?.trim()
  if (!url && !token) return null
  if (!url || !token) throw new Error('Upstash Redis env is incomplete')

  const redis = new Redis({ url, token })
  const interval = new Ratelimit({
    redis,
    limiter: Ratelimit.tokenBucket(1, `${policy.minIntervalMs(env)} ms` as Duration, 1),
    prefix: `wyckoff:${policy.prefix}:interval`,
    ephemeralCache: false,
    timeout: 3000,
  })
  const daily = new Ratelimit({
    redis,
    limiter: Ratelimit.fixedWindow(policy.dailyLimit(env), '1 d'),
    prefix: `wyckoff:${policy.prefix}:daily`,
    ephemeralCache: false,
    timeout: 3000,
  })
  return { check: (userId) => checkUpstash(userId, interval, daily, policy) }
}

async function checkUpstash(
  userId: string,
  interval: Ratelimit,
  daily: Ratelimit,
  policy: RateLimitPolicy,
): Promise<Omit<RateLimitResult, 'mode'>> {
  const shortWindow = await interval.limit(userId)
  if (shortWindow.reason === 'timeout') throw new Error('Redis interval limit timed out')
  if (!shortWindow.success) {
    return denied(
      shortWindow.limit,
      shortWindow.limit - shortWindow.remaining,
      shortWindow.reset,
      policy.intervalMessage,
    )
  }
  const dailyWindow = await daily.limit(userId)
  if (dailyWindow.reason === 'timeout') throw new Error('Redis daily limit timed out')
  if (!dailyWindow.success) {
    return denied(
      dailyWindow.limit,
      dailyWindow.limit - dailyWindow.remaining,
      dailyWindow.reset,
      policy.dailyMessage,
    )
  }
  return {
    ok: true,
    limit: dailyWindow.limit,
    remaining: dailyWindow.remaining,
    reset: dailyWindow.reset,
  }
}

function allowed(limit: number, used: number, reset: number): Omit<RateLimitResult, 'mode'> {
  return { ok: true, limit, remaining: Math.max(limit - used, 0), reset }
}

function denied(
  limit: number,
  used: number,
  reset: number,
  message: string,
): Omit<RateLimitResult, 'mode'> {
  return { ok: false, limit, remaining: Math.max(limit - used, 0), reset, message }
}

function nextUtcDay(timestamp: number): number {
  const date = new Date(timestamp)
  return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + 1)
}

function positiveInt(raw: string | undefined, fallback: number): number {
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? Math.trunc(value) : fallback
}

function setRateLimitHeaders(
  setHeader: (name: string, value: string) => void,
  result: RateLimitResult,
): void {
  setHeader('X-RateLimit-Backend', result.mode)
  setHeader('X-RateLimit-Limit', String(result.limit))
  setHeader('X-RateLimit-Remaining', String(result.remaining))
  setHeader('X-RateLimit-Reset', String(Math.ceil(result.reset / 1000)))
  if (!result.ok) setHeader('Retry-After', String(Math.max(Math.ceil((result.reset - Date.now()) / 1000), 1)))
}
