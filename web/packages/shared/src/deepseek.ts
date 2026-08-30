export const DEEPSEEK_OFFICIAL_ORIGIN = 'https://api.deepseek.com'
export const DEEPSEEK_CONTEXT_WINDOW = 1_000_000
export const DEEPSEEK_REPORT_MAX_OUTPUT_TOKENS = 12_000
export const DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS = 32_768

export type DeepSeekReasoningLevel = 'off' | 'low' | 'high' | 'max'

const LEGACY_MODEL_ALIASES: Record<string, { model: string; reasoningLevel: DeepSeekReasoningLevel }> = {
  'deepseek-chat': { model: 'deepseek-v4-flash', reasoningLevel: 'off' },
  'deepseek-reasoner': { model: 'deepseek-v4-flash', reasoningLevel: 'high' },
}

export function isDeepSeekV4Model(model: string): boolean {
  const id = String(model || '').trim().toLowerCase()
  return id.startsWith('deepseek-v4-flash') || id.startsWith('deepseek-v4-pro')
}

export function isOfficialDeepSeekBaseUrl(baseUrl: string): boolean {
  try {
    return new URL(baseUrl).origin === DEEPSEEK_OFFICIAL_ORIGIN
  } catch {
    return false
  }
}

export function resolveOfficialDeepSeekModel(
  provider: string,
  model: string,
  baseUrl: string,
): { model: string; reasoningLevel?: DeepSeekReasoningLevel; migrated: boolean } {
  if (provider !== 'deepseek' || !isOfficialDeepSeekBaseUrl(baseUrl)) {
    return { model, migrated: false }
  }
  const alias = LEGACY_MODEL_ALIASES[String(model || '').trim().toLowerCase()]
  return alias ? { ...alias, migrated: true } : { model, migrated: false }
}

export function isOfficialDeepSeek(provider: string, model: string, baseUrl: string): boolean {
  const resolved = resolveOfficialDeepSeekModel(provider, model, baseUrl)
  return provider === 'deepseek' && isOfficialDeepSeekBaseUrl(baseUrl) && isDeepSeekV4Model(resolved.model)
}

export function deepSeekThinkingBody(level: DeepSeekReasoningLevel): Record<string, unknown> {
  if (level === 'off') return { thinking: { type: 'disabled' } }
  return { thinking: { type: 'enabled' }, reasoning_effort: level }
}

export function deepSeekResponsesReasoningBody(level: DeepSeekReasoningLevel): Record<string, unknown> {
  return { reasoning: { effort: level === 'off' ? 'none' : level } }
}
