export const PROVIDERS = [
  '1route', 'gemini', 'openai', 'deepseek', 'anthropic',
] as const

export type Provider = (typeof PROVIDERS)[number]

export const PROVIDER_LABELS: Record<Provider, string> = {
  '1route': '1Route（推荐）',
  gemini: 'Gemini',
  openai: '兼容OpenAI协议',
  deepseek: 'DeepSeek',
  anthropic: '兼容Anthropic协议',
}

export const PROVIDER_BASE_URLS: Record<Provider, string> = {
  '1route': 'https://api.1route.dev/v1',
  gemini: '',
  openai: 'https://api.openai.com/v1',
  deepseek: 'https://api.deepseek.com/v1',
  anthropic: '',
}

export const ALLOWED_MODEL_BASE_URLS = [
  PROVIDER_BASE_URLS['1route'],
  'https://www.1route.dev/v1',
  'https://generativelanguage.googleapis.com/v1beta/openai',
  PROVIDER_BASE_URLS.openai,
  PROVIDER_BASE_URLS.deepseek,
  'https://api.anthropic.com',
  'http://token.thegun.cn:8317',
  'https://ark.cn-beijing.volces.com/api/v3',
  'https://ark.cn-beijing.volces.com/api/coding/v3',
] as const

export const ALLOWED_PROXY_TARGET_ORIGINS = [
  'https://api.1route.dev',
  'https://www.1route.dev',
  'https://api.openai.com',
  'https://generativelanguage.googleapis.com',
  'https://api.deepseek.com',
  'https://api.anthropic.com',
  'https://token-plan-sgp.xiaomimimo.com',
  'http://token.thegun.cn:8317',
  'https://api.tickflow.org',
  'https://api.tushare.pro',
  'https://ark.cn-beijing.volces.com',
] as const

const ALLOWED_MODEL_BASE_URL_SET = new Set(ALLOWED_MODEL_BASE_URLS.map(normalizeBaseUrl))
const ALLOWED_MODEL_ORIGINS = new Set([
  'https://api.1route.dev',
  'https://www.1route.dev',
  'https://api.openai.com',
  'https://generativelanguage.googleapis.com',
  'https://api.deepseek.com',
  'https://api.anthropic.com',
  'http://token.thegun.cn:8317',
])

export function isAllowedModelBaseUrl(raw: string): boolean {
  try {
    const url = new URL(raw)
    return ALLOWED_MODEL_BASE_URL_SET.has(normalizeBaseUrl(url.href)) || ALLOWED_MODEL_ORIGINS.has(url.origin)
  } catch {
    return false
  }
}

function normalizeBaseUrl(raw: string): string {
  return raw.replace(/\/+$/, '')
}

export const PROVIDER_DEFAULT_MODELS: Record<Provider, string> = {
  '1route': 'gpt-5.5',
  gemini: 'gemini-2.0-flash',
  openai: 'gpt-4o',
  deepseek: 'deepseek-chat',
  anthropic: 'claude-sonnet-4-20250514',
}

export const TABLE_NAMES = {
  USER_SETTINGS: 'user_settings',
  PORTFOLIOS: 'portfolios',
  PORTFOLIO_POSITIONS: 'portfolio_positions',
  TRADE_ORDERS: 'trade_orders',
  RECOMMENDATION_TRACKING: 'recommendation_tracking',
  TAIL_BUY_HISTORY: 'tail_buy_history',
} as const
