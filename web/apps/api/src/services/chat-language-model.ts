import { createAnthropic } from '@ai-sdk/anthropic'
import { createOpenAI } from '@ai-sdk/openai'
import type { LanguageModel, ToolSet } from 'ai'
import {
  DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
  DEEPSEEK_OFFICIAL_ORIGIN,
  deepSeekResponsesReasoningBody,
  deepSeekThinkingBody,
  isOfficialDeepSeek,
} from '@wyckoff/shared'

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
  maxOutputTokens?: number
}

export function supportsDeepSeekWebSearch(provider: string, model: string, baseUrl = ''): boolean {
  return isOfficialDeepSeek(provider, model, baseUrl)
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
    return DEEPSEEK_OFFICIAL_ORIGIN
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
    maxOutputTokens: DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
  }
}

export function patchDeepSeekApiBody(requestUrl: string, body: BodyInit | null | undefined): BodyInit | null | undefined {
  if (typeof body !== 'string') return body
  let url: URL
  try {
    url = new URL(requestUrl)
  } catch {
    return body
  }
  if (url.origin !== DEEPSEEK_OFFICIAL_ORIGIN) return body
  try {
    const parsed = JSON.parse(body) as Record<string, unknown>
    const isResponses = url.pathname.endsWith('/responses')
    const patched = {
      ...parsed,
      ...(isResponses ? deepSeekResponsesReasoningBody('high') : deepSeekThinkingBody('off')),
    }
    if (isResponses) {
      delete patched.temperature
      delete patched.top_p
    }
    return JSON.stringify(patched)
  } catch {
    return body
  }
}
