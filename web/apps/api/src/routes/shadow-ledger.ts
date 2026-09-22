import { Hono } from 'hono'
import { isActivePlanetMember } from '../middleware/planet-membership'
import { createAdminSupabase, createUserSupabase, resolveUserId } from '../middleware/auth'
import type { Env } from '../app'
import {
  buildShadowLedgerPayload,
  loadShadowSnapshot,
  type ShadowAdminClient,
  type ShadowLedgerTier,
} from '../services/shadow-ledger'

export const shadowLedgerRoutes = new Hono<{ Bindings: Env }>()

shadowLedgerRoutes.get('/', async (c) => {
  const asOf = c.req.query('asOf') || undefined
  try {
    const tier = await resolveShadowTier(c.env, c.req.header('Authorization'))
    const snapshot = await loadShadowSnapshot(createAdminSupabase(c.env) as unknown as ShadowAdminClient)
    return c.json(buildShadowLedgerPayload(snapshot, tier, asOf))
  } catch (error) {
    return shadowLedgerError(c, error)
  }
})

export async function resolveShadowTier(env: Env, authorization: string | undefined): Promise<ShadowLedgerTier> {
  if (!authorization?.startsWith('Bearer ')) return 'showcase'
  const token = authorization.slice(7).trim()
  if (!token) return 'showcase'
  const userId = await resolveUserId(env, token)
  if (!userId) return 'showcase'
  const member = await isActivePlanetMember(createUserSupabase(env, token), userId)
  return member ? 'full' : 'showcase'
}

function shadowLedgerError(c: { json: (body: { error: string }, status: 500 | 503) => Response }, error: unknown) {
  const message = error instanceof Error ? error.message : '影子账本读取失败'
  if (message.includes('admin env')) return c.json({ error: '影子账本服务未配置' }, 503)
  return c.json({ error: message || '影子账本读取失败' }, 500)
}
