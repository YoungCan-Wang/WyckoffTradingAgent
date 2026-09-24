import { describe, expect, it } from 'vitest'
import { SPA_HTML_ENTRIES } from '../spa-html-entries'
import { APP_NAV_GROUPS } from '../app-nav'

describe('APP_NAV_GROUPS', () => {
  it('puts shadow account with tracking and attribution under membership', () => {
    const membership = APP_NAV_GROUPS.find((group) => group.titleKey === 'nav.group.membership')
    expect(membership?.items.map((item) => item.to)).toEqual(['/shadow', '/tracking', '/attribution'])
    const core = APP_NAV_GROUPS.find((group) => group.titleKey === 'nav.group.core')
    expect(core?.items.map((item) => item.to)).toEqual(['/chat', '/analysis', '/fib', '/battle', '/portfolio'])
    expect(membership?.items.map((item) => item.to)).not.toContain('/fib')
  })

  it('emits chat.html so production health can curl --fail /chat', () => {
    expect(SPA_HTML_ENTRIES).toContain('chat')
    expect(SPA_HTML_ENTRIES).toContain('shadow')
    expect(SPA_HTML_ENTRIES).toContain('fib')
  })
})
