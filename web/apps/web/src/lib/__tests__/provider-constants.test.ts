import { describe, expect, it } from 'vitest'
import { ALLOWED_MODEL_BASE_URLS, PROVIDER_BASE_URLS, isAllowedModelBaseUrl } from '@wyckoff/shared'

describe('provider constants', () => {
  it('uses the 1Route API gateway as the default base URL', () => {
    expect(PROVIDER_BASE_URLS['1route']).toBe('https://api.1route.dev/v1')
  })

  it('allows Volcengine Ark OpenAI-compatible base URLs', () => {
    expect(ALLOWED_MODEL_BASE_URLS).toEqual(expect.arrayContaining([
      'https://ark.cn-beijing.volces.com/api/v3',
      'https://ark.cn-beijing.volces.com/api/coding/v3',
    ]))
    expect(isAllowedModelBaseUrl('https://ark.cn-beijing.volces.com/api/v3')).toBe(true)
    expect(isAllowedModelBaseUrl('https://ark.cn-beijing.volces.com/api/coding/v3')).toBe(true)
    expect(isAllowedModelBaseUrl('https://ark.cn-beijing.volces.com/api/other')).toBe(false)
  })
})
