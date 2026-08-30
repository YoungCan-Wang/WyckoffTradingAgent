import { describe, expect, it } from 'vitest'
import {
  deepSeekResponsesBaseUrl,
  patchDeepSeekApiBody,
  resolveChatLanguageModel,
  supportsDeepSeekWebSearch,
} from './chat-language-model'

describe('supportsDeepSeekWebSearch', () => {
  it('enables official DeepSeek V4 Flash and Pro models', () => {
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash', 'https://api.deepseek.com/v1')).toBe(true)
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash-2026', 'https://api.deepseek.com/v1')).toBe(true)
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-pro', 'https://api.deepseek.com/v1')).toBe(true)
    expect(supportsDeepSeekWebSearch('deepseek', 'deepseek-chat', 'https://api.deepseek.com/v1')).toBe(false)
    expect(supportsDeepSeekWebSearch('openai', 'deepseek-v4-flash', 'https://api.deepseek.com/v1')).toBe(false)
    expect(
      supportsDeepSeekWebSearch('deepseek', 'deepseek-v4-flash', 'https://ark.cn-beijing.volces.com/api/v3'),
    ).toBe(false)
  })
})

describe('patchDeepSeekApiBody', () => {
  it('enables high reasoning for Responses without ignored sampling fields', () => {
    const body = patchDeepSeekApiBody(
      'https://api.deepseek.com/responses',
      JSON.stringify({ model: 'deepseek-v4-pro', temperature: 0.4, top_p: 0.9 }),
    )
    const parsed = JSON.parse(String(body))

    expect(parsed).toMatchObject({ reasoning: { effort: 'high' } })
    expect(parsed).not.toHaveProperty('thinking')
    expect(parsed).not.toHaveProperty('reasoning_effort')
    expect(parsed).not.toHaveProperty('temperature')
    expect(parsed).not.toHaveProperty('top_p')
  })

  it('disables reasoning for nested Chat Completions calls', () => {
    const body = patchDeepSeekApiBody(
      'https://api.deepseek.com/v1/chat/completions',
      JSON.stringify({ model: 'deepseek-v4-flash' }),
    )

    expect(JSON.parse(String(body))).toMatchObject({ thinking: { type: 'disabled' } })
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

  it('attaches provider web_search on DeepSeek Pro Responses transport', () => {
    const resolved = resolveChatLanguageModel(
      {
        provider: 'deepseek',
        model: 'deepseek-v4-pro',
        api_key: 'test-key',
        base_url: 'https://api.deepseek.com/v1',
      },
      globalThis.fetch,
    )

    expect(resolved.transport).toBe('responses')
    expect(resolved.providerTools).toHaveProperty('web_search')
    expect(resolved.maxOutputTokens).toBe(32768)
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
