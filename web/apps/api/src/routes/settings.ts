import { createAnthropic } from '@ai-sdk/anthropic'
import { createOpenAI } from '@ai-sdk/openai'
import { generateText } from 'ai'
import { Hono } from 'hono'
import { z } from 'zod'
import {
  normalizeTickFlowSymbol,
  PROVIDER_BASE_URLS,
  PROVIDER_DEFAULT_MODELS,
  isAllowedModelBaseUrl,
  type Provider,
} from '@wyckoff/shared'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import type { Env } from '../app'

type SettingsBindings = { Bindings: Env; Variables: { auth: AuthContext } }
type ModelTestConfig = z.infer<typeof MODEL_TEST_SCHEMA>

export const settingsRoutes = new Hono<SettingsBindings>()

settingsRoutes.use('*', authMiddleware)

settingsRoutes.get('/', async (c) => {
  return c.json({ message: 'Settings endpoint - Phase 2' })
})

settingsRoutes.put('/', async (c) => {
  return c.json({ message: 'Settings save endpoint - Phase 2' })
})

settingsRoutes.post('/test-model', async (c) => {
  const body = await c.req.json().catch(() => null)
  const parsed = MODEL_TEST_SCHEMA.safeParse(body)
  if (!parsed.success) return c.json({ ok: false, error: '模型配置不完整' }, 400)

  const config = normalizeModelConfig(parsed.data)
  const baseUrl = allowedProviderBaseUrl(config.provider, config.base_url)
  if (!baseUrl) return c.json({ ok: false, error: '模型 Base URL 不在允许列表内' }, 400)

  try {
    const result = await generateText({
      model: providerModel({ ...config, base_url: baseUrl }),
      prompt: '请仅回复 OK。这是一条 Wyckoff 设置页模型连通性测试请求。',
      temperature: 0,
      maxOutputTokens: 8,
      abortSignal: c.req.raw.signal,
    })
    return c.json({
      ok: true,
      provider: config.provider,
      model: config.model,
      message: '模型连通性正常',
      sample: result.text.slice(0, 40),
    })
  } catch (error) {
    return c.json({ ok: false, error: normalizeConnectivityError(error) }, 502)
  }
})

settingsRoutes.post('/test-data-source', async (c) => {
  const body = await c.req.json().catch(() => null)
  const parsed = DATA_SOURCE_TEST_SCHEMA.safeParse(body)
  if (!parsed.success) return c.json({ ok: false, error: 'TickFlow API Key 不能为空' }, 400)

  try {
    const symbol = normalizeTickFlowSymbol('000001')
    const params = new URLSearchParams({ symbol, period: '1d', count: '1', adjust: 'forward' })
    const response = await fetch(`https://api.tickflow.org/v1/klines?${params}`, {
      headers: { 'x-api-key': parsed.data.tickflow_api_key },
      signal: c.req.raw.signal,
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      return c.json({ ok: false, error: tickFlowError(response.status, payload) }, 502)
    }
    const rows = tickFlowRowCount(payload)
    if (rows <= 0) return c.json({ ok: false, error: 'TickFlow 返回为空，请检查 Key 权限或稍后重试。' }, 502)
    return c.json({ ok: true, source: 'tickflow', symbol, rows, message: 'TickFlow 连通性正常' })
  } catch (error) {
    return c.json({ ok: false, error: normalizeConnectivityError(error) }, 502)
  }
})

const MODEL_TEST_SCHEMA = z.object({
  provider: z.enum(['1route', 'gemini', 'openai', 'deepseek', 'anthropic']),
  api_key: z.string().trim().min(1),
  model: z.string().trim().min(1),
  base_url: z.string().trim().optional().default(''),
})

const DATA_SOURCE_TEST_SCHEMA = z.object({
  tickflow_api_key: z.string().trim().min(1),
})

function normalizeModelConfig(config: ModelTestConfig): Required<ModelTestConfig> {
  const provider = config.provider
  return {
    provider,
    api_key: config.api_key,
    model: config.model || PROVIDER_DEFAULT_MODELS[provider],
    base_url: config.base_url || providerDefaultBaseUrl(provider),
  }
}

function providerDefaultBaseUrl(provider: Provider): string {
  if (provider === 'gemini') return 'https://generativelanguage.googleapis.com/v1beta/openai'
  if (provider === 'anthropic') return 'https://api.anthropic.com'
  return PROVIDER_BASE_URLS[provider]
}

function allowedProviderBaseUrl(provider: Provider, raw: string): string {
  const baseUrl = raw || providerDefaultBaseUrl(provider)
  return isAllowedModelBaseUrl(baseUrl) ? baseUrl : ''
}

function providerModel(config: Required<ModelTestConfig>) {
  if (config.provider === 'anthropic') {
    return createAnthropic({ apiKey: config.api_key, baseURL: config.base_url }).chat(config.model)
  }
  return createOpenAI({ apiKey: config.api_key, baseURL: config.base_url }).chat(config.model)
}

function tickFlowRowCount(payload: unknown): number {
  if (!payload || typeof payload !== 'object') return 0
  const obj = payload as Record<string, unknown>
  if (Array.isArray(obj.data)) return obj.data.length
  if (Array.isArray(obj.records)) return obj.records.length
  return tickFlowTableRowCount(obj.data) || tickFlowTableRowCount(obj)
}

function tickFlowTableRowCount(raw: unknown): number {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return 0
  const table = raw as Record<string, unknown>
  if (Array.isArray(table.timestamp)) return table.timestamp.length
  for (const value of Object.values(table)) {
    const rows = tickFlowTableRowCount(value)
    if (rows > 0) return rows
  }
  return 0
}

function tickFlowError(status: number, payload: unknown): string {
  const message = payload && typeof payload === 'object'
    ? String((payload as { message?: unknown; error?: unknown }).message || (payload as { error?: unknown }).error || '')
    : ''
  return `TickFlow 连通性失败（HTTP ${status}${message ? `: ${message}` : ''}）`
}

function normalizeConnectivityError(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return String(error || '连通性测试失败')
}
