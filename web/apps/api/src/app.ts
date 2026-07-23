import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import { cors } from 'hono/cors'
import { requestId } from 'hono/request-id'
import { secureHeaders } from 'hono/secure-headers'
import type { AgentRunMessage } from './services/agent-run'

export type Env = {
  SUPABASE_URL?: string
  SUPABASE_ANON_KEY?: string
  SUPABASE_SERVICE_ROLE_KEY?: string
  VITE_SUPABASE_URL?: string
  VITE_SUPABASE_ANON_KEY?: string
  TICKFLOW_API_BASE?: string
  CHAT_DAILY_LIMIT_PER_USER?: string
  CHAT_MIN_INTERVAL_MS?: string
  CHAT_TOOL_APPROVAL_SECRET?: string
  UPSTASH_REDIS_REST_URL?: string
  UPSTASH_REDIS_REST_TOKEN?: string
  AGENT_SANDBOX_ENABLED?: string
  AGENT_SANDBOX_TIMEOUT_MS?: string
  AGENT_RUN_TTL_SECONDS?: string
  AGENT_RUN_QUEUE?: Queue<AgentRunMessage>
  SANDBOX_BRIDGE_URL?: string
  SANDBOX_BRIDGE_SECRET?: string
}

export function createApiApp() {
  const app = new Hono<{ Bindings: Env }>()

  app.use('*', requestId({ limitLength: 128 }))
  app.use('*', secureHeaders())
  app.use('*', cors({
    origin: [
      'http://localhost:5173',
      'http://localhost:5174',
      'http://localhost:5175',
      'http://127.0.0.1:5173',
      'http://127.0.0.1:5174',
      'http://127.0.0.1:5175',
      'https://wyckoff-analysis.pages.dev',
      'https://wyckoff.pages.dev',
    ],
    credentials: true,
  }))
  app.use('/api/*', bodyLimit({
    maxSize: 256 * 1024,
    onError: (c) => c.json({ error: 'Request body is too large', requestId: c.get('requestId') }, 413),
  }))

  app.onError((_error, c) => c.json({ error: 'Internal Server Error', requestId: c.get('requestId') }, 500))
  app.notFound((c) => c.json({ error: 'Not Found', requestId: c.get('requestId') }, 404))
  app.get('/api/health', (c) => c.json({ status: 'ok' }))
  return app
}
