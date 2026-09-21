import { describe, expect, it } from 'vitest'
import { APP_NAV_GROUPS } from '../app-nav'

describe('APP_NAV_GROUPS', () => {
  it('puts shadow account with tracking and attribution under membership', () => {
    const membership = APP_NAV_GROUPS.find((group) => group.titleKey === 'nav.group.membership')
    expect(membership?.items.map((item) => item.to)).toEqual(['/shadow', '/tracking', '/attribution'])
    const core = APP_NAV_GROUPS.find((group) => group.titleKey === 'nav.group.core')
    expect(core?.items.map((item) => item.to)).toEqual(['/chat', '/analysis', '/battle', '/portfolio'])
  })
})
