import { createMiddleware } from 'hono/factory'
import type { Env } from '../app'
import { createUserSupabase, type AuthContext } from './auth'

export const planetMemberMiddleware = createMiddleware<{
  Bindings: Env
  Variables: { auth: AuthContext }
}>(async (c, next) => {
  const auth = c.get('auth')
  const supabase = createUserSupabase(c.env, auth.accessToken)
  if (!(await isActivePlanetMember(supabase, auth.userId))) {
    return c.json({ error: 'Planet membership required' }, 403)
  }
  await next()
})

export async function isActivePlanetMember(
  supabase: ReturnType<typeof createUserSupabase>,
  userId: string,
): Promise<boolean> {
  const { data, error } = await supabase
    .from('planet_members')
    .select('expires_on')
    .eq('user_id', userId)
    .limit(1)
  if (error || !Array.isArray(data)) return false
  return data.some((row) => isActiveExpiry(row.expires_on))
}

function isActiveExpiry(value: unknown): boolean {
  if (value == null || String(value).trim() === '') return true
  const expiry = String(value).trim()
  const date = new Date(`${expiry}T00:00:00Z`)
  const isIsoDate = /^\d{4}-\d{2}-\d{2}$/.test(expiry)
    && !Number.isNaN(date.valueOf())
    && date.toISOString().slice(0, 10) === expiry
  return isIsoDate && expiry >= new Date().toISOString().slice(0, 10)
}
