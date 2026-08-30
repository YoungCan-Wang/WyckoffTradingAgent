export const DEEPSEEK_OFFICIAL_ORIGIN = 'https://api.deepseek.com'
export const DEEPSEEK_CONTEXT_WINDOW = 1_000_000
export const DEEPSEEK_REPORT_MAX_OUTPUT_TOKENS = 12_000
export const DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS = 32_768

export type DeepSeekReasoningLevel = 'off' | 'low' | 'high' | 'max'

export function isDeepSeekV4Model(model: string): boolean {
  const id = String(model || '').trim().toLowerCase()
  return id.startsWith('deepseek-v4-flash') || id.startsWith('deepseek-v4-pro')
}

export function isOfficialDeepSeek(provider: string, model: string, baseUrl: string): boolean {
  if (provider !== 'deepseek' || !isDeepSeekV4Model(model)) return false
  try {
    return new URL(baseUrl).origin === DEEPSEEK_OFFICIAL_ORIGIN
  } catch {
    return false
  }
}

export function deepSeekThinkingBody(level: DeepSeekReasoningLevel): Record<string, unknown> {
  if (level === 'off') return { thinking: { type: 'disabled' } }
  return { thinking: { type: 'enabled' }, reasoning_effort: level }
}

export function deepSeekResponsesReasoningBody(level: Exclude<DeepSeekReasoningLevel, 'off'>): Record<string, unknown> {
  return { reasoning: { effort: level } }
}
