import { describe, expect, it } from 'vitest'
import { isPlanetMembershipActive, planetMembershipToday } from '../planet-membership'

describe('planet membership expiry', () => {
  it('treats null and blank expiry as permanent', () => {
    expect(isPlanetMembershipActive(null, '2026-06-30')).toBe(true)
    expect(isPlanetMembershipActive('', '2026-06-30')).toBe(true)
    expect(isPlanetMembershipActive('   ', '2026-06-30')).toBe(true)
  })

  it('keeps membership active through its expiry date', () => {
    expect(isPlanetMembershipActive('2026-06-30', '2026-06-30')).toBe(true)
    expect(isPlanetMembershipActive('2026-07-01', '2026-06-30')).toBe(true)
  })

  it('rejects expired or malformed expiry values', () => {
    expect(isPlanetMembershipActive('2026-06-29', '2026-06-30')).toBe(false)
    expect(isPlanetMembershipActive('20260630', '2026-06-30')).toBe(false)
    expect(isPlanetMembershipActive('2026-13-01', '2026-06-30')).toBe(false)
    expect(isPlanetMembershipActive('2026-02-30', '2026-06-30')).toBe(false)
  })

  it('uses the Asia/Shanghai calendar day on both sides of midnight', () => {
    expect(planetMembershipToday(new Date('2026-06-30T15:59:59Z'))).toBe('2026-06-30')
    expect(planetMembershipToday(new Date('2026-06-30T16:00:00Z'))).toBe('2026-07-01')
  })
})
