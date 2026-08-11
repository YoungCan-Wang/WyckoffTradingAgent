import { createAnthropic } from '@ai-sdk/anthropic'
import { createOpenAI } from '@ai-sdk/openai'
import type { LanguageModel, ToolSet } from 'ai'

export type ChatLanguageModelConfig = {
  provider: string
  model: string
  api_key: string
  base_url: string
  protocol?: 'openai' | 'anthropic'
}

export type ResolvedChatLanguageModel = {
  model: LanguageModel
  nestedModel: LanguageModel
  providerTools: ToolSet
  transport: 'chat' | 'responses'
}

const DEEPSEEK_RESPONSES_ORIGIN = 'https://api.deepseek.com'

export function supportsDeepSeekWebSearch(provider: string, model: string, baseUrl = ''): boolean {
  if (provider !== 'deepseek') return false
  try {
    if (new URL(baseUrl).origin !== DEEPSEEK_RESPONSES_ORIGIN) return false
  } catch {
    return false
  }
  const id = String(model || '').trim().toLowerCase()
  return id === 'deepseek-v4-flash' || id.startsWith('deepseek-v4-flash-')
}

/** DeepSeek Responses docs use root origin; chat completions keep `/v1`. */
export function deepSeekResponsesBaseUrl(chatBaseUrl: string): string {
  try {
    const url = new URL(chatBaseUrl)
    const path = url.pathname.replace(/\/+$/, '')
    if (path === '/v1' || path.endsWith('/v1')) {
      url.pathname = path.slice(0, -3) || '/'
      const trimmedPath = url.pathname.replace(/\/+$/, '')
      return trimmedPath && trimmedPath !== '/' ? `${url.origin}${trimmedPath}` : url.origin
    }
    return `${url.origin}${path || ''}`.replace(/\/+$/, '') || url.origin
  } catch {
    return DEEPSEEK_RESPONSES_ORIGIN
  }
}

export function resolveChatLanguageModel(
  config: ChatLanguageModelConfig,
  fetchImpl: typeof globalThis.fetch,
): ResolvedChatLanguageModel {
  if (config.protocol === 'anthropic') {
    const provider = createAnthropic({ apiKey: config.api_key, baseURL: config.base_url, fetch: fetchImpl })
    const model = provider.chat(config.model)
    return { model, nestedModel: model, providerTools: {}, transport: 'chat' }
  }

  const nestedProvider = createOpenAI({
    apiKey: config.api_key,
    baseURL: config.base_url,
    fetch: fetchImpl,
  })
  const nestedModel = nestedProvider.chat(config.model)
  if (!supportsDeepSeekWebSearch(config.provider, config.model, config.base_url)) {
    return { model: nestedModel, nestedModel, providerTools: {}, transport: 'chat' }
  }

  const provider = createOpenAI({
    apiKey: config.api_key,
    baseURL: deepSeekResponsesBaseUrl(config.base_url),
    fetch: fetchImpl,
  })
  return {
    model: provider.responses(config.model),
    nestedModel,
    providerTools: { web_search: provider.tools.webSearch({}) } as ToolSet,
    transport: 'responses',
  }
}
