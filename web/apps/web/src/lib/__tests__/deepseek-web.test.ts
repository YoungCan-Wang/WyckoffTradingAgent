import { describe, expect, it, vi } from 'vitest'
import {
  buildDeepSeekWebTools,
  deepseekWebFetch,
  deepseekWebSearch,
  officialDeepSeekWebCredential,
  standaloneDeepSeekWebEnabled,
  DEEPSEEK_WEB_FETCH_MAX_CHARS,
} from '@wyckoff/shared'

const OFFICIAL = { apiKey: 'sk-test', baseUrl: 'https://api.deepseek.com/v1' }
const HIT = {
  rerank_score: 2.4,
  title: 'DeepSeek V4 模型发布',
  url: 'https://example.com/deepseek-v4',
  snippet: '短摘要',
  summary: '长摘要给模型读',
  content: '',
  site_name: 'Example News',
  site_icon: 'https://example.com/favicon.ico',
  date: '2026-06-01T00:00:00+00:00',
  authority: 3,
}

function jsonFetch(status: number, body: unknown): { fetchImpl: typeof fetch; calls: Array<[string, RequestInit]> } {
  const calls: Array<[string, RequestInit]> = []
  const fetchImpl = (async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    calls.push([String(input), init ?? {}])
    return Response.json(body, { status })
  }) as typeof fetch
  return { fetchImpl, calls }
}

async function runTool(tools: ReturnType<typeof buildDeepSeekWebTools>, name: string, input: unknown) {
  const execute = (tools[name] as unknown as { execute: (value: unknown, options: unknown) => Promise<unknown> }).execute
  return execute(input, { toolCallId: 'call-1', messages: [] })
}

describe('deepseekWebSearch', () => {
  it('posts the official search contract and maps a hit', async () => {
    const { fetchImpl, calls } = jsonFetch(200, { results: [HIT] })
    const result = await deepseekWebSearch({
      ...OFFICIAL,
      fetchImpl,
      query: '  DeepSeek V4 模型 发布 ',
      count: 5,
      oriQuestion: 'V4 什么时候发布',
      localization: '1.2.3.4',
    })

    expect(result).toMatchObject({ ok: true, empty: false, results: [expect.objectContaining({
      title: HIT.title,
      url: HIT.url,
      snippet: HIT.snippet,
      summary: HIT.summary,
      rerank_score: 2.4,
      authority: 3,
      site_name: 'Example News',
      date: HIT.date,
    })] })
    expect(calls[0]?.[0]).toBe('https://api.deepseek.com/v1/tools/search')
    expect(calls[0]?.[1]).toMatchObject({
      method: 'POST',
      redirect: 'manual',
      headers: {
        Authorization: 'Bearer sk-test',
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
    })
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({
      query: 'DeepSeek V4 模型 发布',
      count: 5,
      ori_question: 'V4 什么时候发布',
      localization: '1.2.3.4',
    })
  })

  it('treats an empty result list as success and does not ask for page text', async () => {
    const { fetchImpl, calls } = jsonFetch(200, { results: [] })
    const result = await deepseekWebSearch({ ...OFFICIAL, fetchImpl, query: '不存在的词' })
    expect(result).toEqual({ ok: true, results: [], empty: true })
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ query: '不存在的词' })
  })

  it('returns the upstream 4xx without retrying', async () => {
    const { fetchImpl } = jsonFetch(400, {
      error: { message: "'count' must be between 1 and 50", type: 'invalid_request_error', param: null, code: 'invalid_request_error' },
    })
    await expect(deepseekWebSearch({ ...OFFICIAL, fetchImpl, query: 'DeepSeek', count: 10 })).resolves.toEqual({
      ok: false,
      status: 400,
      code: 'invalid_request',
      message: "'count' must be between 1 and 50",
      retryable: false,
    })
  })

  it('marks 5xx and 429 as retryable and keeps the response graceful', async () => {
    const server = jsonFetch(500, { error: { message: 'internal', type: 'server_error', param: null, code: 'server_error' } })
    const limited = jsonFetch(429, { error: { message: 'rate limit', type: 'rate_limit_error', param: null, code: 'rate_limit' } })
    await expect(deepseekWebSearch({ ...OFFICIAL, fetchImpl: server.fetchImpl, query: 'DeepSeek' })).resolves.toMatchObject({
      ok: false, status: 500, code: 'upstream', retryable: true,
    })
    await expect(deepseekWebSearch({ ...OFFICIAL, fetchImpl: limited.fetchImpl, query: 'DeepSeek' })).resolves.toMatchObject({
      ok: false, status: 429, code: 'rate_limited', message: 'rate limit', retryable: true,
    })
  })

  it('times out without throwing', async () => {
    const fetchImpl = ((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => {
        reject(init.signal?.reason ?? new DOMException('timeout', 'TimeoutError'))
      })
    })) as typeof fetch
    const result = await deepseekWebSearch({ ...OFFICIAL, fetchImpl, query: 'DeepSeek', searchTimeoutMs: 20 })
    expect(result).toMatchObject({ ok: false, status: null, code: 'timeout', retryable: true })
  })

  it('rejects a bad query or proxy origin before any request', async () => {
    const fetchImpl = vi.fn() as unknown as typeof fetch
    const blank = await deepseekWebSearch({ ...OFFICIAL, fetchImpl, query: '   ' })
    const proxy = await deepseekWebSearch({
      apiKey: 'sk-test',
      baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
      fetchImpl,
      query: 'DeepSeek',
    })
    expect(blank).toMatchObject({ ok: false, code: 'invalid_request', status: null })
    expect(proxy).toMatchObject({ ok: false, code: 'unconfigured' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe('deepseekWebFetch', () => {
  it('posts one http url and returns markdown', async () => {
    const { fetchImpl, calls } = jsonFetch(200, { title: 'Example article', content: '# Example article\n\nArticle content.' })
    const result = await deepseekWebFetch({ ...OFFICIAL, fetchImpl, url: 'https://example.com/article' })
    expect(result).toEqual({
      ok: true,
      title: 'Example article',
      content: '# Example article\n\nArticle content.',
      truncated: false,
      empty: false,
    })
    expect(calls[0]?.[0]).toBe('https://api.deepseek.com/v1/tools/fetch')
    expect(JSON.parse(String(calls[0]?.[1].body))).toEqual({ url: 'https://example.com/article' })
  })

  it('caps markdown and reports truncation', async () => {
    const { fetchImpl } = jsonFetch(200, { title: 'Long', content: 'a'.repeat(DEEPSEEK_WEB_FETCH_MAX_CHARS + 25) })
    const result = await deepseekWebFetch({ ...OFFICIAL, fetchImpl, url: 'https://example.com/long' })
    expect(result).toMatchObject({ ok: true, truncated: true, empty: false })
    if (result.ok) expect(result.content).toHaveLength(DEEPSEEK_WEB_FETCH_MAX_CHARS)
  })

  it('maps fetch 404 and credentialed urls to non-retryable errors', async () => {
    const missing = jsonFetch(404, {
      error: { message: 'no extractable content', type: 'invalid_request_error', param: null, code: 'not_found' },
    })
    await expect(deepseekWebFetch({ ...OFFICIAL, fetchImpl: missing.fetchImpl, url: 'https://example.com/missing' })).resolves.toMatchObject({
      ok: false, status: 404, code: 'not_found', retryable: false,
    })
    const fetchImpl = vi.fn() as unknown as typeof fetch
    await expect(deepseekWebFetch({ ...OFFICIAL, fetchImpl, url: 'https://user:secret@example.com/a' })).resolves.toMatchObject({
      ok: false, code: 'invalid_request',
    })
    expect(fetchImpl).not.toHaveBeenCalled()
  })
})

describe('deepseek web tools', () => {
  it('caps count, hides the key, and omits empty page text', async () => {
    const { fetchImpl, calls } = jsonFetch(200, { results: [HIT] })
    const tools = buildDeepSeekWebTools(OFFICIAL, fetchImpl)
    const output = await runTool(tools, 'deepseek_web_search', {
      query: 'DeepSeek',
      count: 50,
      ori_question: null,
      localization: null,
    })
    expect(output).toMatchObject({
      count: 10,
      empty: false,
      billed: true,
      results: [{ title: HIT.title, url: HIT.url, summary: HIT.summary }],
    })
    expect(JSON.stringify(output)).not.toContain('site_icon')
    expect(JSON.stringify(output)).not.toContain('sk-test')
    expect(JSON.parse(String(calls[0]?.[1].body)).count).toBe(10)
    expect(JSON.parse(String(calls[0]?.[1].body))).not.toHaveProperty('need_content')
  })

  it('returns an empty search and a fetch failure as data', async () => {
    const search = jsonFetch(200, { results: [] })
    const searchTools = buildDeepSeekWebTools(OFFICIAL, search.fetchImpl)
    await expect(runTool(searchTools, 'deepseek_web_search', {
      query: '无结果', count: null, ori_question: null, localization: null,
    })).resolves.toMatchObject({ empty: true, billed: false, note: '没有匹配结果，本次不计费。' })

    const fetchTools = buildDeepSeekWebTools(OFFICIAL, jsonFetch(502, {}).fetchImpl)
    await expect(runTool(fetchTools, 'deepseek_web_fetch', { url: 'https://example.com/down' })).resolves.toMatchObject({
      code: 'upstream', status: 502, retryable: true,
    })
  })

  it('enables standalone tools only without Responses web_search and only for the official key', () => {
    const official = officialDeepSeekWebCredential([
      { provider: 'openai', api_key: 'sk-openai', base_url: 'https://api.openai.com/v1' },
      { provider: 'deepseek', api_key: 'sk-official', base_url: 'https://api.deepseek.com/v1' },
    ])
    const proxy = officialDeepSeekWebCredential([
      { provider: 'deepseek', api_key: 'sk-proxy', base_url: 'https://ark.cn-beijing.volces.com/api/v3' },
    ])
    expect(official).toEqual({ apiKey: 'sk-official', baseUrl: 'https://api.deepseek.com/v1' })
    expect(proxy).toBeNull()
    expect(standaloneDeepSeekWebEnabled(true, official)).toBe(false)
    expect(standaloneDeepSeekWebEnabled(false, official)).toBe(true)
    expect(standaloneDeepSeekWebEnabled(false, null)).toBe(false)
  })
})
