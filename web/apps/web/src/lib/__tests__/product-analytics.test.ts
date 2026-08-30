import { afterEach, describe, expect, it, vi } from 'vitest'
import { installCloudflareWebAnalytics, installPlanetMemberClarity } from '../product-analytics'

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('product analytics installers', () => {
  it('injects the Cloudflare Web Analytics beacon when a token is configured', () => {
    vi.stubEnv('VITE_CF_WEB_ANALYTICS_TOKEN', 'cf-token')
    const doc = fakeDocument()
    installCloudflareWebAnalytics(doc as unknown as Document)
    const script = doc.getElementById('cf-web-analytics')
    expect(script?.src).toContain('cloudflareinsights.com/beacon.min.js')
    expect(script?.getAttribute('data-cf-beacon')).toContain('cf-token')
    installCloudflareWebAnalytics(doc as unknown as Document)
    expect(doc.scripts).toHaveLength(1)
  })

  it('does not inject Clarity for empty users', () => {
    const doc = fakeDocument()
    installPlanetMemberClarity('', doc as unknown as Document)
    expect(doc.getElementById('ms-clarity')).toBeUndefined()
  })

  it('uses the production Clarity project when no env override is set', () => {
    const doc = fakeDocument()
    const win = {} as Window
    installPlanetMemberClarity('user-1', doc as unknown as Document, win)
    expect(doc.getElementById('ms-clarity')?.src).toContain('clarity.ms/tag/y6albpfin1')
  })

  it('loads Clarity only once and identifies the planet member', () => {
    vi.stubEnv('VITE_CLARITY_PROJECT_ID', 'clarity-id')
    const doc = fakeDocument()
    const win = {} as Window
    installPlanetMemberClarity('user-1', doc as unknown as Document, win)
    installPlanetMemberClarity('user-1', doc as unknown as Document, win)
    expect(doc.scripts).toHaveLength(1)
    expect(doc.getElementById('ms-clarity')?.src).toContain('clarity.ms/tag/clarity-id')
    expect((win as Window & { clarity?: { q?: unknown[] } }).clarity?.q).toEqual([['identify', 'user-1']])
  })
})

function fakeDocument() {
  const nodes = new Map<string, FakeScript>()
  const scripts: FakeScript[] = []
  return {
    scripts,
    getElementById: (id: string) => nodes.get(id),
    createElement: (tag: string) => {
      if (tag !== 'script') throw new Error(`unexpected tag ${tag}`)
      return new FakeScript()
    },
    head: {
      appendChild: (node: FakeScript) => {
        nodes.set(node.id, node)
        scripts.push(node)
      },
    },
  }
}

class FakeScript {
  id = ''
  src = ''
  defer = false
  async = false
  private attrs = new Map<string, string>()

  setAttribute(name: string, value: string) {
    this.attrs.set(name, value)
  }

  getAttribute(name: string) {
    return this.attrs.get(name)
  }
}
