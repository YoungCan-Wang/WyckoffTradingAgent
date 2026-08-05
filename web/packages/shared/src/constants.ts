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
])

export function isAllowedModelBaseUrl(raw: string): boolean {
  try {
    const url = new URL(raw)
    return ALLOWED_MODEL_BASE_URL_SET.has(normalizeBaseUrl(url.href)) || ALLOWED_MODEL_ORIGINS.has(url.origin)
  } catch {
    return false
  }
}

export function isSafeProviderBaseUrl(raw: string): boolean {
  try {
    const url = new URL(raw)
    if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443')) return false
    const host = url.hostname.toLowerCase().replace(/^\[|\]$/g, '')
    if (host === 'localhost' || host.endsWith('.localhost') || host === '::1' || host === '0:0:0:0:0:0:0:1') return false
    if (/^(10\.|127\.|169\.254\.|192\.168\.)/.test(host)) return false
    const match = host.match(/^172\.(\d{1,3})\./)
    if (match && Number(match[1]) >= 16 && Number(match[1]) <= 31) return false
    if (/^(0\.|224\.|240\.)/.test(host) || host.startsWith('fc') || host.startsWith('fd') || host.startsWith('fe8') || host.startsWith('fe9') || host.startsWith('fea') || host.startsWith('feb')) return false
    return Boolean(host)
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
  deepseek: 'deepseek-v4-flash',
  anthropic: 'claude-sonnet-4-20250514',
}

export const TABLE_NAMES = {
  USER_SETTINGS: 'user_settings',
  PORTFOLIOS: 'portfolios',
  PORTFOLIO_POSITIONS: 'portfolio_positions',
  TRADE_ORDERS: 'trade_orders',
  RECOMMENDATION_TRACKING: 'recommendation_tracking',
} as const
