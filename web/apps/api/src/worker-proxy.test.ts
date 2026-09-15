import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_WORKER_ORIGIN,
  handleWorkerProxyRequest,
  isWorkerProxyPath,
  proxyToWorker,
  resolveWorkerOrigin,
  workerProxyUrl,
} from './worker-proxy'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('worker proxy routing', () => {
  it('allowlists Worker API prefixes and rejects Pages-only paths', () => {
    expect(isWorkerProxyPath('/api/chat')).toBe(true)
    expect(isWorkerProxyPath('/api/chat/config')).toBe(true)
    expect(isWorkerProxyPath('/api/settings/test-model')).toBe(true)
    expect(isWorkerProxyPath('/api/portfolio')).toBe(true)
    expect(isWorkerProxyPath('/api/agent-runs/ws')).toBe(true)
    expect(isWorkerProxyPath('/api/remote/ws')).toBe(true)
    expect(isWorkerProxyPath('/api/health')).toBe(true)
    expect(isWorkerProxyPath('/api/llm-proxy/v1/chat/completions')).toBe(false)
    expect(isWorkerProxyPath('/api/news-events')).toBe(false)
    expect(isWorkerProxyPath('/api/chatter')).toBe(false)
  })

  it('rewrites the incoming Pages URL onto the Worker origin', () => {
    expect(workerProxyUrl(
      'https://wyckoff-analysis.pages.dev/api/chat/config?x=1',
      DEFAULT_WORKER_ORIGIN,
    )).toBe(`${DEFAULT_WORKER_ORIGIN}/api/chat/config?x=1`)
  })

  it('uses the public Worker origin unless Pages overrides it', () => {
    expect(resolveWorkerOrigin()).toBe(DEFAULT_WORKER_ORIGIN)
    expect(resolveWorkerOrigin({ WYCKOFF_API_ORIGIN: ' https://example.workers.dev/ ' }))
      .toBe('https://example.workers.dev')
  })
})

describe('proxyToWorker', () => {
  it('forwards the original request so auth and streams stay intact', async () => {
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: hi\n\n'))
        controller.close()
      },
    })
    const fetchMock = vi.fn(async () => new Response(stream, {
      headers: { 'content-type': 'text/event-stream' },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const request = new Request('https://wyckoff-analysis.pages.dev/api/chat', {
      method: 'POST',
      headers: {
        Authorization: 'Bearer test-token',
        Accept: 'text/event-stream',
      },
      body: '{"ok":true}',
    })
    const response = await proxyToWorker(request)

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`${DEFAULT_WORKER_ORIGIN}/api/chat`)
    expect(fetchMock.mock.calls[0]?.[1]).toBe(request)
    expect(response.headers.get('content-type')).toBe('text/event-stream')
    expect(await response.text()).toBe('data: hi\n\n')
  })

  it('does not proxy Pages-only routes such as llm-proxy', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const response = await handleWorkerProxyRequest(
      new Request('https://wyckoff-analysis.pages.dev/api/llm-proxy/v1/chat/completions'),
    )

    expect(response.status).toBe(404)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
