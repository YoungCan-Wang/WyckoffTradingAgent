import { createMiddleware } from 'hono/factory'
import { createClient } from '@supabase/supabase-js'
import type { Env } from '../app'

export type AuthContext = {
  userId: string
  accessToken: string
}

export function createUserSupabase(env: Env, accessToken: string) {
  const url = env.SUPABASE_URL || env.VITE_SUPABASE_URL
  const key = env.SUPABASE_ANON_KEY || env.VITE_SUPABASE_ANON_KEY
  if (!url || !key) throw new Error('Supabase env is missing')
  return createClient(url, key, { global: { headers: { Authorization: `Bearer ${accessToken}` } } })
}

export const authMiddleware = createMiddleware<{
  Bindings: Env
  Variables: { auth: AuthContext }
}>(async (c, next) => {
  const authHeader = c.req.header('Authorization')
  if (!authHeader?.startsWith('Bearer ')) {
    return c.json({ error: 'Unauthorized' }, 401)
  }

  const token = authHeader.slice(7)
  const url = c.env.SUPABASE_URL || c.env.VITE_SUPABASE_URL
  const anonKey = c.env.SUPABASE_ANON_KEY || c.env.VITE_SUPABASE_ANON_KEY
  if (!url || !anonKey) return c.json({ error: 'Supabase env is missing' }, 500)
  const supabase = createClient(url, anonKey)

  const { data: { user }, error } = await supabase.auth.getUser(token)
  if (error || !user) {
    return c.json({ error: 'Invalid token' }, 401)
  }

  c.set('auth', { userId: user.id, accessToken: token })
  await next()
})
