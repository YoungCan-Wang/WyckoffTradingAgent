import { describe, expect, it, vi } from 'vitest'
import { readingRoomDeepSeekWeb } from './chat-deepseek-web'
import { resolveChatLanguageModel } from './chat-language-model'

const OFFICIAL = { provider: 'deepseek', api_key: 'sk-official', base_url: 'https://api.deepseek.com/v1' }

describe('readingRoomDeepSeekWeb', () => {
  it('leaves the Responses web_search path without a second paid search tool', () => {
    const resolved = resolveChatLanguageModel({
      provider: 'deepseek',
      model: 'deepseek-v4-flash',
      api_key: 'sk-official',
      base_url: 'https://api.deepseek.com/v1',
    }, globalThis.fetch)

    expect(resolved.transport).toBe('responses')
    expect(resolved.providerTools).toHaveProperty('web_search')

    const web = readingRoomDeepSeekWeb({
      configs: [OFFICIAL],
      providerToolNames: Object.keys(resolved.providerTools),
      fetchImpl: vi.fn() as unknown as typeof fetch,
    })
    expect(web).toEqual({ tools: {}, guidance: '' })
  })

  it('registers search and fetch for other models when an official DeepSeek key is saved', () => {
    const web = readingRoomDeepSeekWeb({
      configs: [
        { provider: 'openai', api_key: 'sk-openai', base_url: 'https://api.openai.com/v1' },
        OFFICIAL,
      ],
      providerToolNames: [],
      fetchImpl: vi.fn() as unknown as typeof fetch,
    })

    expect(Object.keys(web.tools).sort()).toEqual(['deepseek_web_fetch', 'deepseek_web_search'])
    expect(web.guidance).toContain('deepseek_web_search')
    expect(web.guidance).toContain('deepseek_web_fetch')
    expect(web.guidance).not.toContain('sk-official')
  })

  it('does not enable the tools for a proxy DeepSeek base URL or a blank key', () => {
    const proxy = readingRoomDeepSeekWeb({
      configs: [{ provider: 'deepseek', api_key: 'sk-proxy', base_url: 'https://ark.cn-beijing.volces.com/api/v3' }],
      providerToolNames: [],
      fetchImpl: vi.fn() as unknown as typeof fetch,
    })
    const blank = readingRoomDeepSeekWeb({
      configs: [{ provider: 'deepseek', api_key: '   ', base_url: 'https://api.deepseek.com/v1' }],
      providerToolNames: [],
      fetchImpl: vi.fn() as unknown as typeof fetch,
    })

    expect(proxy.tools).toEqual({})
    expect(blank.tools).toEqual({})
  })
})
