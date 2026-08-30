import { createMiddleware } from 'hono/factory'
import { isPlanetMembershipActive } from '@wyckoff/shared'
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
  return data.some((row) => isPlanetMembershipActive(row.expires_on))
}
