import { afterEach, describe, expect, it, vi } from 'vitest'
import { chatApiPath, fetchAgentLane } from '@/features/reading-room/chat-state'

describe('chatApiPath', () => {
  it('sends members in the agent lane to their own endpoint and everyone else to the free lane', () => {
    expect(chatApiPath(true)).toBe('/api/agent/chat')
    expect(chatApiPath(false)).toBe('/api/chat')
  })
})

describe('fetchAgentLane', () => {
  afterEach(() => vi.unstubAllGlobals())

  function stubFetch(impl: () => Promise<Response>) {
    const fetchMock = vi.fn(impl)
    vi.stubGlobal('fetch', fetchMock)
    return fetchMock
  }

  it('asks the server and sends the bearer token', async () => {
    const fetchMock = stubFetch(async () => Response.json({ enabled: true }))

    await expect(fetchAgentLane('token-1')).resolves.toBe(true)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toMatch(/\/api\/agent\/config$/)
    expect(init.headers).toEqual({ Authorization: 'Bearer token-1' })
  })

  it('stays in the free lane when the server says no', async () => {
    stubFetch(async () => Response.json({ enabled: false }))
    await expect(fetchAgentLane('t')).resolves.toBe(false)
  })

  it.each([
    ['a non-boolean answer', async () => Response.json({ enabled: 'yes' })],
    ['an error status', async () => new Response('nope', { status: 503 })],
    ['an unparsable body', async () => new Response('<html>', { status: 200 })],
    ['a network failure', async () => { throw new TypeError('Failed to fetch') }],
  ])('falls back to the free lane on %s', async (_name, impl) => {
    stubFetch(impl)
    await expect(fetchAgentLane('t')).resolves.toBe(false)
  })
})
