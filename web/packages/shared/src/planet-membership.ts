export const PLANET_MEMBERSHIP_TIME_ZONE = 'Asia/Shanghai'

export function planetMembershipToday(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: PLANET_MEMBERSHIP_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}

export function isPlanetMembershipActive(expiresOn: unknown, today = planetMembershipToday()): boolean {
  if (expiresOn == null || String(expiresOn).trim() === '') return true
  const value = String(expiresOn).trim()
  return isIsoDate(value) && value >= today
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value
}
