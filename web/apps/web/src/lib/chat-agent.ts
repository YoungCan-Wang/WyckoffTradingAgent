import { supabase } from './supabase'
import {
  DEEPSEEK_CONTEXT_WINDOW,
  PROVIDER_BASE_URLS,
  PROVIDER_DEFAULT_MODELS,
  resolveOfficialDeepSeekModel,
  type DeepSeekReasoningLevel,
  type Provider,
} from '@wyckoff/shared'

export interface LLMConfig {
  provider?: string
  api_key: string
  model: string
  base_url: string
  protocol?: 'openai' | 'anthropic'
  reasoning_level?: DeepSeekReasoningLevel
}

const RETIRED_PROVIDERS = new Set(['zhipu', 'minimax', 'qwen', 'volcengine'])
const ALLOWED_URL_RE = /^https?:\/\//i
const UNKNOWN_MODEL_CONTEXT_WINDOW = 64_000
const COMPACT_RESERVE_RATIO = 0.25
const MIN_COMPACT_RESERVE_TOKENS = 16_384
const MAX_COMPACT_RESERVE_TOKENS = 32_768
const TAIL_KEEP = 4
const DEFAULT_RECENT_KEEP_TOKENS = 20_000
const MIN_RECENT_KEEP_TOKENS = 4_000

type ChatHistoryMessage = { role: 'user' | 'assistant'; content: string }

export interface PreparedChatHistory {
  messages: ChatHistoryMessage[]
  compacted: boolean
  beforeTokens: number
  afterTokens: number
  beforeMessages: number
  afterMessages: number
}

type UserSettingsRow = {
  chat_provider?: string | null
  gemini_api_key?: string | null
  gemini_model?: string | null
  gemini_base_url?: string | null
  openai_api_key?: string | null
  openai_model?: string | null
  openai_base_url?: string | null
  deepseek_api_key?: string | null
  deepseek_model?: string | null
  deepseek_base_url?: string | null
  anthropic_api_key?: string | null
  anthropic_model?: string | null
  anthropic_base_url?: string | null
  custom_providers?: unknown
}

const MODEL_CONTEXT_WINDOWS: [string, number][] = [
  ['deepseek-v4', DEEPSEEK_CONTEXT_WINDOW],
  ['deepseek', 64_000],
  ['gpt-4o', 128_000],
  ['gpt-4', 128_000],
  ['gpt-3.5', 16_000],
  ['gemini-3', 128_000],
  ['gemini-2', 1_000_000],
  ['gemini', 128_000],
  ['claude-opus', 200_000],
  ['claude-sonnet', 200_000],
  ['claude', 200_000],
  ['minimax', 128_000],
  ['kimi', 128_000],
  ['qwen', 128_000],
  ['longcat', 64_000],
  ['mistral', 128_000],
  ['step', 64_000],
]

export function getChatContextWindow(modelName: string): number {
  const lower = modelName.toLowerCase()
  return MODEL_CONTEXT_WINDOWS.find(([prefix]) => lower.includes(prefix))?.[1] ?? UNKNOWN_MODEL_CONTEXT_WINDOW
}

function getChatCompactReserveTokens(contextWindow: number): number {
  const window = Math.max(contextWindow, 1)
  const ratioReserve = Math.floor(window * COMPACT_RESERVE_RATIO)
  const clampedReserve = Math.max(MIN_COMPACT_RESERVE_TOKENS, Math.min(ratioReserve, MAX_COMPACT_RESERVE_TOKENS))
  return Math.min(clampedReserve, Math.floor(window / 2))
}

export function getChatCompactThreshold(modelName: string): number {
  const window = getChatContextWindow(modelName)
  return Math.max(1, window - getChatCompactReserveTokens(window))
}

export function getChatRecentKeepTokens(modelName: string): number {
  const threshold = getChatCompactThreshold(modelName)
  if (threshold <= MIN_RECENT_KEEP_TOKENS * 2) return Math.max(1_000, Math.floor(threshold / 2))
  return Math.min(DEFAULT_RECENT_KEEP_TOKENS, Math.max(MIN_RECENT_KEEP_TOKENS, Math.floor(threshold / 2)))
}

function estimateChatMessageTokens(message: ChatHistoryMessage): number {
  const content = message.content || ''
  const bytes = new TextEncoder().encode(content).length
  return Math.max(Math.floor(content.length / 2), Math.floor(bytes / 3), 1)
}

function estimateChatTokens(messages: ChatHistoryMessage[]): number {
  return messages.reduce((total, message) => total + estimateChatMessageTokens(message), 0)
}

function findChatTailStartByTokenBudget(messages: ChatHistoryMessage[], keepRecentTokens: number): number {
  if (messages.length === 0) return 0
  const minTailStart = Math.max(0, messages.length - TAIL_KEEP)
  let accumulated = 0
  let tailStart = minTailStart

  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i]
    if (!message) continue
    accumulated += estimateChatMessageTokens(message)
    if (accumulated >= keepRecentTokens) {
      tailStart = i
      break
    }
  }

  return Math.min(tailStart, minTailStart)
}

function buildLocalChatSummary(messages: ChatHistoryMessage[], maxChars = 1200): string {
  const codes: string[] = []
  const userGoals: string[] = []
  const assistantNotes: string[] = []

  for (const message of messages) {
    for (const code of message.content.match(/\b\d{6}\b/g) || []) {
      if (!codes.includes(code)) codes.push(code)
    }
    if (message.role === 'user') userGoals.push(message.content.slice(0, 180))
    if (message.role === 'assistant') assistantNotes.push(message.content.slice(0, 220))
  }

  const lines = ['前序读盘室对话已压缩为摘要。']
  if (codes.length) lines.push(`涉及标的：${codes.slice(0, 12).join(', ')}`)
  if (userGoals.length) {
    lines.push('用户关注：')
    for (const item of userGoals.slice(-6)) lines.push(`- ${item}`)
  }
  if (assistantNotes.length) {
    lines.push('已给出的主要结论：')
    for (const item of assistantNotes.slice(-6)) lines.push(`- ${item}`)
  }

  const summary = lines.join('\n')
  return summary.length <= maxChars ? summary : `${summary.slice(0, maxChars - 1).trimEnd()}…`
}

export function prepareChatMessagesForModel(messages: ChatHistoryMessage[], modelName: string): PreparedChatHistory {
  const normalized = messages
    .filter((message) => message.content.trim())
    .map((message) => ({ role: message.role, content: message.content }))
  const beforeTokens = estimateChatTokens(normalized)
  const beforeMessages = normalized.length

  if (normalized.length <= TAIL_KEEP + 2 || beforeTokens <= getChatCompactThreshold(modelName)) {
    return {
      messages: normalized,
      compacted: false,
      beforeTokens,
      afterTokens: beforeTokens,
      beforeMessages,
      afterMessages: beforeMessages,
    }
  }

  const tailStart = findChatTailStartByTokenBudget(normalized, getChatRecentKeepTokens(modelName))
  if (tailStart <= 2) {
    return {
      messages: normalized,
      compacted: false,
      beforeTokens,
      afterTokens: beforeTokens,
      beforeMessages,
      afterMessages: beforeMessages,
    }
  }

  const summary = buildLocalChatSummary(normalized.slice(0, tailStart))
  const compactedMessages: ChatHistoryMessage[] = [
    {
      role: 'user',
      content: `[读盘室对话摘要]\n${summary}\n\n[系统说明] 以上是前序读盘室对话摘要。后续回答可以结合摘要和保留的最近对话，但当前持仓、价格、行情和策略结果仍必须以工具实时返回为准。`,
    },
    { role: 'assistant', content: '好的，我已接续前序读盘室上下文。' },
    ...normalized.slice(tailStart),
  ]

  return {
    messages: compactedMessages,
    compacted: true,
    beforeTokens,
    afterTokens: estimateChatTokens(compactedMessages),
    beforeMessages,
    afterMessages: compactedMessages.length,
  }
}

function parseCustomProviders(raw: unknown): Record<string, Record<string, string>> {
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {})
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const result: Record<string, Record<string, string>> = {}
    for (const [key, value] of Object.entries(parsed)) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) continue
      const entry = value as Record<string, unknown>
      const baseUrl = String(entry.baseurl || entry.base_url || '')
      if (baseUrl && !ALLOWED_URL_RE.test(baseUrl)) continue
      result[key] = Object.fromEntries(Object.entries(entry).map(([k, v]) => [k, String(v ?? '')]))
    }
    return result
  } catch {
    return {}
  }
}

export async function loadLLMConfig(userId: string): Promise<LLMConfig | null> {
  const configs = await loadLLMConfigCandidates(userId)
  return configs[0] || null
}

export async function loadLLMConfigCandidates(userId: string): Promise<LLMConfig[]> {
  const { data } = await supabase
    .from('user_settings')
    .select('chat_provider, gemini_api_key, gemini_model, gemini_base_url, openai_api_key, openai_model, openai_base_url, deepseek_api_key, deepseek_model, deepseek_base_url, anthropic_api_key, anthropic_model, anthropic_base_url, custom_providers')
    .eq('user_id', userId)
    .single()

  if (!data) return []

  const settings = data as UserSettingsRow
  const activeProvider = settings.chat_provider || '1route'
  const custom = parseCustomProviders(settings.custom_providers)
  const providers = Array.from(new Set([
    activeProvider,
    'openai',
    'deepseek',
    'gemini',
    'anthropic',
    ...Object.keys(custom),
  ]))
  return providers
    .filter((provider) => !RETIRED_PROVIDERS.has(provider))
    .map((provider) => buildProviderConfig(provider, settings))
    .filter((config) => Boolean(config.api_key && config.model && config.base_url))
}

const BUILTIN_PROVIDER_OVERRIDES: Record<string, Partial<LLMConfig>> = {
  gemini: { base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', protocol: 'openai' },
  anthropic: { base_url: 'https://api.anthropic.com', protocol: 'anthropic' },
}

function buildProviderConfig(provider: string, data: UserSettingsRow): LLMConfig {
  const apiKey = data[`${provider}_api_key` as keyof UserSettingsRow]
  if (typeof apiKey === 'string' && apiKey) {
    const model = data[`${provider}_model` as keyof UserSettingsRow] as string | null | undefined
    const baseUrl = data[`${provider}_base_url` as keyof UserSettingsRow] as string | null | undefined
    const override = BUILTIN_PROVIDER_OVERRIDES[provider]
    return normalizeDeepSeekConfig({
      provider,
      api_key: apiKey,
      model: model || PROVIDER_DEFAULT_MODELS[provider as Provider] || '',
      base_url: baseUrl || override?.base_url || PROVIDER_BASE_URLS[provider as Provider] || '',
      protocol: override?.protocol || 'openai',
    })
  }
  const custom = parseCustomProviders(data.custom_providers)
  const info = custom[provider] || {}
  return normalizeDeepSeekConfig({
    provider,
    api_key: info.apikey || info.api_key || '',
    model: info.model || PROVIDER_DEFAULT_MODELS[provider as Provider] || '',
    base_url: info.baseurl || info.base_url || PROVIDER_BASE_URLS[provider as Provider] || '',
    protocol: 'openai',
  })
}

function normalizeDeepSeekConfig(config: LLMConfig): LLMConfig {
  const resolved = resolveOfficialDeepSeekModel(config.provider || '', config.model, config.base_url)
  return {
    ...config,
    model: resolved.model,
    ...(resolved.reasoningLevel ? { reasoning_level: resolved.reasoningLevel } : {}),
  }
}
