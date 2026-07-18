import { createMiddleware } from 'hono/factory'
import type { Env } from '../app'
import { createUserSupabase, type AuthContext } from './auth'

export const whitelistMiddleware = createMiddleware<{
  Bindings: Env
  Variables: { auth: AuthContext }
}>(async (c, next) => {
  const auth = c.get('auth')
  const supabase = createUserSupabase(c.env, auth.accessToken)
  if (!(await isActiveWhitelistUser(supabase, auth.userId))) {
    return c.json({ error: 'Whitelist required' }, 403)
  }
  await next()
})

export async function isActiveWhitelistUser(
  supabase: ReturnType<typeof createUserSupabase>,
  userId: string,
): Promise<boolean> {
  const { data, error } = await supabase.from('whitelist').select('expire_date').eq('user_id', userId).limit(1)
  if (error || !Array.isArray(data)) return false
  return data.some((row) => {
    const expiry = String(row.expire_date || '').trim()
    return !expiry || (/^\d{8}$/.test(expiry) && expiry >= compactToday())
  })
}

function compactToday(): string {
  return new Date().toISOString().slice(0, 10).replace(/-/g, '')
}
