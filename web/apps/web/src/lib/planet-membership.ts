import { supabase } from './supabase'
import { isPlanetMembershipActive } from '@wyckoff/shared'

export { isPlanetMembershipActive, planetMembershipToday } from '@wyckoff/shared'

export interface PlanetMembership {
  isActive: boolean
  expiresOn: string | null
  joinedAt: string | null
}

export async function getPlanetMembership(userId: string): Promise<PlanetMembership> {
  const { data, error } = await supabase
    .from('planet_members')
    .select('created_at, expires_on')
    .eq('user_id', userId)
    .maybeSingle()
  if (error) throw new Error(`会员状态读取失败：${error.message}`)
  if (!data) return { isActive: false, expiresOn: null, joinedAt: null }
  return {
    isActive: isPlanetMembershipActive(data.expires_on),
    expiresOn: typeof data.expires_on === 'string' ? data.expires_on : null,
    joinedAt: typeof data.created_at === 'string' ? data.created_at : null,
  }
}
