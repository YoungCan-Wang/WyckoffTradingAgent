import { describe, expect, it } from 'vitest'
import {
  deepSeekResponsesBaseUrl,
  resolveChatLanguageModel,
  supportsDeepSeekWebSearch,
} from './chat-language-model'

describe('supportsDeepSeekWebSearch', () => {
  it('enables only official DeepSeek origin + v4 flash models', () => {
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash', 'https://api.deepseek.com/v1')).toBe(true)
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash-2026', 'https://api.deepseek.com/v1')).toBe(true)
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-chat', 'https://api.deepseek.com/v1')).toBe(false)
    expect(supportsDeepSeekWebSearch('openai', 'deepseek-v4-flash', 'https://api.deepseek.com/v1')).toBe(false)
    expect(
      supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash', 'https://ark.cn-beijing.volces.com/api/v3'),
    ).toBe(false)
  })
})

describe('deepSeekResponsesBaseUrl', () => {
  it('strips trailing /v1 for Responses root origin', () => {
    expect(deepSeekResponsesBaseUrl('https://api.deepseek.com/v1')).toBe('https://api.deepseek.com')
    expect(deepSeekResponsesBaseUrl('https://api.deepseek.com/v1/')).toBe('https://api.deepseek.com')
  })
})

describe('resolveChatLanguageModel', () => {
  it('attaches provider web_search on DeepSeek Responses transport', () => {
    const resolved = resolveChatLanguageModel(
      {
        provider: 'deepseek',
        model: 'deepseek-v4-flash',
        api_key: 'test-key',
        base_url: 'https://api.deepseek.com/v1',
      },
      globalThis.fetch,
    )

    expect(resolved.transport).toBe('responses')
    expect(resolved.providerTools).toHaveProperty('web_search')
    expect(resolved.model).not.toBe(resolved.nestedModel)
  })

  it('keeps chat transport without web_search for other DeepSeek models', () => {
    const resolved = resolveChatLanguageModel(
      {
        provider: 'deepseek',
        model: 'deepseek-chat',
        api_key: 'test-key',
        base_url: 'https://api.deepseek.com/v1',
      },
      globalThis.fetch,
    )

    expect(resolved.transport).toBe('chat')
    expect(resolved.providerTools).toEqual({})
    expect(resolved.model).toBe(resolved.nestedModel)
  })

  it('keeps chat transport when deepseek base_url is a proxy origin', () => {
    const resolved = resolveChatLanguageModel(
      {
        provider: 'deepseek',
        model: 'deepseek-v4-flash',
        api_key: 'test-key',
        base_url: 'https://ark.cn-beijing.volces.com/api/v3',
      },
      globalThis.fetch,
    )

    expect(resolved.transport).toBe('chat')
    expect(resolved.providerTools).toEqual({})
  })
})
