import { supabase } from './supabase'

export interface PlanetMembership {
  isActive: boolean
  expiresOn: string | null
  joinedAt: string | null
}

export function planetMembershipToday(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

export function isPlanetMembershipActive(expiresOn: unknown, today = planetMembershipToday()): boolean {
  if (expiresOn == null || String(expiresOn).trim() === '') return true
  const value = String(expiresOn).trim()
  return isIsoDate(value) && value >= today
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

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value
}
