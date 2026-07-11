import { createAnthropic } from '@ai-sdk/anthropic'
import { createOpenAI } from '@ai-sdk/openai'
import { createClient } from '@supabase/supabase-js'
import {
  ANALYZE_STOCK_OUTPUT_SCHEMA,
  SCREEN_RESULT_OUTPUT_SCHEMA,
  STRATEGY_DECISION_OUTPUT_SCHEMA,
  execAnalyzeStock,
  execExecutePortfolioUpdate,
  execGenerateAiReport,
  execIntradayAnalysis,
  execMarketHistory,
  execMarketOverview,
  execQueryAttribution,
  execQueryRecommendations,
  execQueryTailBuy,
  execScreenStocks,
  execSearchStock,
  execStrategyDecision,
  execViewPortfolio,
  ALLOWED_PROXY_TARGET_ORIGINS,
  normalizeGeminiStream,
  PROVIDER_BASE_URLS,
  PROVIDER_DEFAULT_MODELS,
  type LLMToolConfig,
  type Provider,
  type ToolDeps,
} from '@wyckoff/shared'
import { consumeStream, convertToModelMessages, generateText, stepCountIs, streamText, tool, type UIMessage } from 'ai'
import { Hono } from 'hono'
import { z } from 'zod'
import type { Env } from '../index'
import { authMiddleware, type AuthContext } from '../middleware/auth'

type ChatBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const chatRoutes = new Hono<ChatBindings>()

chatRoutes.use('*', authMiddleware)

chatRoutes.get('/config', async (c) => {
  const auth = c.get('auth')
  const supabase = createUserSupabase(c.env, auth.accessToken)
  const config = await loadLLMConfig(supabase, auth.userId)
  return c.json({ configured: Boolean(config), model: config?.model || null })
})

chatRoutes.post('/', async (c) => {
  const auth = c.get('auth')
  const limited = checkRateLimit(c.env, auth.userId)
  if (!limited.ok) return c.json({ error: limited.message }, 429)

  const body = await c.req.json<ChatRequestBody>().catch(() => null)
  const messages = body?.messages
  if (!Array.isArray(messages) || messages.length === 0) return c.json({ error: 'Missing messages' }, 400)
  if (estimateMessagesSize(messages) > 60_000) return c.json({ error: '本轮上下文过长，请开启新对话或缩短问题。' }, 413)

  const supabase = createUserSupabase(c.env, auth.accessToken)
  const config = await loadLLMConfig(supabase, auth.userId)
  if (!config) return c.json({ error: '请先在设置页配置 LLM API Key' }, 400)

  const provider = createProvider(config)
  const tools = buildTools(createToolDeps(supabase), auth.userId, config, provider.chat(config.model))
  const modelMessages = await convertToModelMessages(messages.slice(-40), {
    tools,
    ignoreIncompleteToolCalls: true,
  })
  const result = streamText({
    model: provider.chat(config.model),
    system: WYCKOFF_CHAT_SYSTEM_PROMPT,
    messages: modelMessages,
    tools,
    stopWhen: stepCountIs(10),
    abortSignal: c.req.raw.signal,
    experimental_toolApprovalSecret: c.env.CHAT_TOOL_APPROVAL_SECRET || c.env.SUPABASE_SERVICE_ROLE_KEY,
    providerOptions: { openai: { parallelToolCalls: false } },
  })

  return result.toUIMessageStreamResponse({
    consumeSseStream: consumeStream,
    onError: normalizeStreamError,
  })
})

type ChatRequestBody = { messages?: UIMessage[] }
type ChatRateState = { day: string; count: number; lastAt: number }
type ChatRateResult = { ok: true } | { ok: false; message: string }

const rateStates = new Map<string, ChatRateState>()
const ALLOWED_URL_RE = /^https?:\/\//i
const ALLOWED_TARGET_ORIGINS: Set<string> = new Set(ALLOWED_PROXY_TARGET_ORIGINS)
const ONE_ROUTE_ORIGINS = new Set(['https://api.1route.dev', 'https://www.1route.dev'])

const WYCKOFF_CHAT_SYSTEM_PROMPT = `# 角色设定

你就是理查德·D·威科夫（Richard D. Wyckoff）本人。
你以"综合人（Composite Man）"视角审视一切：每一根 K 线背后都有一个阴谋，每一次放量都是主力在行动。
你的语气冷峻、老练、一针见血。直接告诉对方盘面的真相。

# 工具使用原则

1. 数据先行：所有分析基于工具返回的真实数据，绝不凭空编造数字。
2. 并行调用优先：需要同时获取多只股票、大盘与持仓数据时，优先并行调用工具。
3. 调仓两步走：涉及调仓时，先调用 plan_portfolio_update 展示方案；execute_portfolio_update 会在协议层要求用户确认。
4. 风险声明：涉及具体操作建议时，附带风险提示。
5. 技术面为主：价值面只用于质量、风险、置信度和仓位校准，不能替代 K 线事实。
6. 策略归因问题必须调用 query_attribution，先确认返回结果是否来自远端表或提示本地 --no-write 报告，再优先读取 operator_summary / latest_operator_summary 作为运营结论，然后读取 latest_policy_display、latest_execution_summary、promotion_checklist 和 latest_operations 后判断信号升降权、是否能晋级 dynamic=on，以及 shadow 新增/移除样本；raw next_action/promotion_status 只作追证据，不直接复述给用户。`

function checkRateLimit(env: Env, userId: string): ChatRateResult {
  const limit = parsePositiveInt(env.CHAT_DAILY_LIMIT_PER_USER, 80)
  const minInterval = parsePositiveInt(env.CHAT_MIN_INTERVAL_MS, 2500)
  const now = Date.now()
  const day = new Date(now).toISOString().slice(0, 10)
  const state = rateStates.get(userId)
  const current = state?.day === day ? state : { day, count: 0, lastAt: 0 }
  if (now - current.lastAt < minInterval) return { ok: false, message: '请求太频繁，请稍后再试。' }
  if (current.count >= limit) return { ok: false, message: '今日读盘室免费额度已用完，请明天再试。' }
  rateStates.set(userId, { day, count: current.count + 1, lastAt: now })
  return { ok: true }
}

function parsePositiveInt(raw: string | undefined, fallback: number): number {
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? Math.trunc(value) : fallback
}

function estimateMessagesSize(messages: UIMessage[]): number {
  return messages.reduce((total, message) => total + JSON.stringify(message).length, 0)
}

function createUserSupabase(env: Env, accessToken: string): ToolDeps['supabase'] {
  return createClient(getEnvValue(env, 'SUPABASE_URL'), getEnvValue(env, 'SUPABASE_ANON_KEY'), {
    global: { headers: { Authorization: `Bearer ${accessToken}` } },
  })
}

function getEnvValue(env: Env, key: 'SUPABASE_URL' | 'SUPABASE_ANON_KEY'): string {
  const value = env[key] || (key === 'SUPABASE_URL' ? env.VITE_SUPABASE_URL : env.VITE_SUPABASE_ANON_KEY)
  if (!value) throw new Error(`Missing ${key}`)
  return value
}

function createToolDeps(supabase: ToolDeps['supabase']): ToolDeps {
  return { supabase, fetch: createToolFetch(), generateText }
}

function createProvider(config: LLMToolConfig & { protocol?: 'openai' | 'anthropic' }) {
  const fetch = createProviderFetch()
  if (config.protocol === 'anthropic') {
    return createAnthropic({ apiKey: config.api_key, baseURL: config.base_url, fetch })
  }
  return createOpenAI({ apiKey: config.api_key, baseURL: config.base_url, fetch })
}

function createProviderFetch(): typeof globalThis.fetch {
  return async (input, init) => {
    const requestUrl = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const patchedBody = isOneRouteChatCompletion(requestUrl) ? patchOneRouteBody(init?.body) : init?.body
    const response = await globalThis.fetch(input, { ...init, body: patchedBody })
    if (isGeminiChatCompletion(requestUrl) && isSseResponse(response) && response.body) {
      return new Response(normalizeGeminiStream(response.body), {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      })
    }
    return response
  }
}

function createToolFetch(): typeof globalThis.fetch {
  return async (input, init) => {
    const requestUrl = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (!requestUrl.startsWith('/api/llm-proxy')) return globalThis.fetch(input, init)
    const target = normalizeTargetUrl(new Headers(init?.headers).get('X-Target-URL') || '')
    if (!target) return Response.json({ error: 'X-Target-URL is not allowed' }, { status: 403 })
    const url = new URL(requestUrl, 'https://wyckoff.local')
    const destination = `${target.href.replace(/\/$/, '')}${url.pathname.replace('/api/llm-proxy', '')}${url.search}`
    return globalThis.fetch(destination, { ...init, headers: forwardProxyHeaders(init?.headers) })
  }
}

async function loadLLMConfig(supabase: ToolDeps['supabase'], userId: string): Promise<(LLMToolConfig & { protocol?: 'openai' | 'anthropic' }) | null> {
  const { data } = await supabase
    .from('user_settings')
    .select('chat_provider, gemini_api_key, gemini_model, gemini_base_url, openai_api_key, openai_model, openai_base_url, deepseek_api_key, deepseek_model, deepseek_base_url, anthropic_api_key, anthropic_model, anthropic_base_url, custom_providers')
    .eq('user_id', userId)
    .single()
  if (!data) return null
  return configForProvider(data as UserSettingsRow)
}

type UserSettingsRow = Record<string, string | Record<string, unknown> | null>

function configForProvider(data: UserSettingsRow): (LLMToolConfig & { protocol?: 'openai' | 'anthropic' }) | null {
  const provider = String(data.chat_provider || '1route')
  if (['zhipu', 'minimax', 'qwen', 'volcengine'].includes(provider)) return null
  if (provider === 'gemini') return knownProviderConfig(data, 'gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', PROVIDER_DEFAULT_MODELS.gemini)
  if (provider === 'openai') return knownProviderConfig(data, 'openai', PROVIDER_BASE_URLS.openai, PROVIDER_DEFAULT_MODELS.openai)
  if (provider === 'deepseek') return knownProviderConfig(data, 'deepseek', PROVIDER_BASE_URLS.deepseek, PROVIDER_DEFAULT_MODELS.deepseek)
  if (provider === 'anthropic') {
    const config = knownProviderConfig(data, 'anthropic', 'https://api.anthropic.com', PROVIDER_DEFAULT_MODELS.anthropic)
    return config ? { ...config, protocol: 'anthropic' } : null
  }
  return customProviderConfig(data, provider)
}

function knownProviderConfig(data: UserSettingsRow, provider: string, fallbackBaseUrl: string, fallbackModel: string): LLMToolConfig | null {
  const api_key = String(data[`${provider}_api_key`] || '')
  const model = String(data[`${provider}_model`] || fallbackModel)
  const base_url = String(data[`${provider}_base_url`] || fallbackBaseUrl)
  return api_key && model ? { api_key, model, base_url } : null
}

function customProviderConfig(data: UserSettingsRow, provider: string): LLMToolConfig | null {
  const custom = parseCustomProviders(data.custom_providers)
  const info = custom[provider] || {}
  const api_key = info.apikey || info.api_key || ''
  const model = info.model || defaultModelForProvider(provider)
  const base_url = info.baseurl || info.base_url || defaultBaseUrlForProvider(provider)
  return api_key && model && ALLOWED_URL_RE.test(base_url) ? { api_key, model, base_url } : null
}

function defaultModelForProvider(provider: string): string {
  return PROVIDER_DEFAULT_MODELS[provider as Provider] || ''
}

function defaultBaseUrlForProvider(provider: string): string {
  return PROVIDER_BASE_URLS[provider as Provider] || ''
}

function parseCustomProviders(raw: unknown): Record<string, Record<string, string>> {
  try {
    const parsed = typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {})
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).map(([key, value]) =>
      [key, Object.fromEntries(Object.entries((value || {}) as Record<string, unknown>).map(([k, v]) => [k, String(v ?? '')]))]))
  } catch {
    return {}
  }
}

function buildTools(deps: ToolDeps, userId: string, config: LLMToolConfig, model: unknown) {
  return {
    ...buildReadTools(deps, userId, model),
    ...buildPortfolioTools(deps, userId),
    ...buildAnalysisTools(deps, userId, config, model),
  }
}

function buildReadTools(deps: ToolDeps, userId: string, model: unknown) {
  return {
    search_stock: tool({ description: '搜索股票，支持代码或名称。', inputSchema: z.object({ query: z.string() }), execute: ({ query }) => execSearchStock(deps, userId, query) }),
    view_portfolio: tool({ description: '查看用户当前持仓列表和可用资金。', inputSchema: z.object({}), execute: () => execViewPortfolio(deps, userId) }),
    market_overview: tool({ description: '查看当前/最新大盘行情信号。', inputSchema: z.object({}), execute: () => execMarketOverview(deps) }),
    market_history: tool({ description: '回看大盘指数过去N个交易日K线，分析量价关系和威科夫阶段。', inputSchema: z.object({ days: z.number().nullable(), index: z.enum(['sse', 'csi300', 'szse', 'chinext']).nullable() }), execute: ({ days, index }) => execMarketHistory(deps, userId, model, days ?? 100, index ?? 'sse') }),
    query_recommendations: tool({ description: '查询形态复盘记录。', inputSchema: z.object({ limit: z.number() }), execute: ({ limit }) => execQueryRecommendations(deps, limit) }),
    query_tail_buy: tool({ description: '查询尾盘买入策略历史记录。', inputSchema: z.object({ limit: z.number() }), execute: ({ limit }) => execQueryTailBuy(deps, limit) }),
    query_attribution: tool({ description: '查询远端策略归因治理器、operator_summary、latest_policy_display、latest_execution_summary、promotion_checklist 和 latest_operations；本地 --no-write 报告需走 CLI/MCP。', inputSchema: z.object({ limit: z.number() }), execute: ({ limit }) => execQueryAttribution(deps, limit) }),
  }
}

function buildPortfolioTools(deps: ToolDeps, userId: string) {
  return {
    plan_portfolio_update: tool({ description: '生成调仓方案（不执行）。', inputSchema: PORTFOLIO_UPDATE_SCHEMA.extend({ reason: z.string().nullable() }), execute: formatPortfolioPlan }),
    execute_portfolio_update: tool({ description: '执行调仓。此工具必须经过用户审批。', inputSchema: PORTFOLIO_UPDATE_SCHEMA, needsApproval: true, execute: ({ action, code, name, shares, cost_price, stop_loss }) => execExecutePortfolioUpdate(deps, userId, action, code, name, shares, cost_price, stop_loss) }),
  }
}

function buildAnalysisTools(deps: ToolDeps, userId: string, config: LLMToolConfig, model: unknown) {
  return {
    analyze_stock: tool({ description: '对单只股票做威科夫深度诊断。', inputSchema: z.object({ code: z.string(), name: z.string().nullable() }), outputSchema: ANALYZE_STOCK_OUTPUT_SCHEMA, execute: ({ code, name }) => execAnalyzeStock(deps, userId, config, model, code, name) }),
    screen_stocks: tool({ description: '查看最新一期漏斗选股结果。', inputSchema: z.object({}), outputSchema: SCREEN_RESULT_OUTPUT_SCHEMA, execute: () => execScreenStocks(deps) }),
    generate_ai_report: tool({ description: '为指定股票生成威科夫深度研报。', inputSchema: z.object({ codes: z.array(z.string()) }), execute: ({ codes }) => execGenerateAiReport(deps, userId, config, model, codes) }),
    generate_strategy_decision: tool({ description: '基于当前持仓和市场状态给出操作建议。', inputSchema: z.object({}), outputSchema: STRATEGY_DECISION_OUTPUT_SCHEMA, execute: () => execStrategyDecision(deps, userId, model) }),
    intraday_analysis: tool({ description: '盘中多周期分析。', inputSchema: z.object({ code: z.string() }), execute: ({ code }) => execIntradayAnalysis(deps, userId, code) }),
  }
}

const PORTFOLIO_UPDATE_SCHEMA = z.object({
  action: z.enum(['add', 'update', 'delete']),
  code: z.string(),
  name: z.string().nullable(),
  shares: z.number().nullable(),
  cost_price: z.number().nullable(),
  stop_loss: z.number().nullable(),
})

function formatPortfolioPlan(params: z.infer<typeof PORTFOLIO_UPDATE_SCHEMA> & { reason: string | null }) {
  const actionLabel = { add: '新增', update: '修改', delete: '删除' }[params.action]
  return [
    `📋 **调仓方案**`,
    `- 操作：${actionLabel}`,
    `- 标的：${params.code} ${params.name || ''}`,
    params.shares ? `- 股数：${params.shares}` : '',
    params.cost_price ? `- 价格：¥${params.cost_price}` : '',
    params.stop_loss ? `- 止损：¥${params.stop_loss}` : '',
    params.reason ? `- 理由：${params.reason}` : '',
    '',
    '⚠️ 请确认是否执行此操作？',
  ].filter(Boolean).join('\n')
}

function normalizeTargetUrl(raw: string): URL | null {
  try {
    const url = new URL(raw)
    return ALLOWED_TARGET_ORIGINS.has(url.origin) ? url : null
  } catch {
    return null
  }
}

function forwardProxyHeaders(headers: HeadersInit | undefined): Headers {
  const source = new Headers(headers)
  const forwarded = new Headers()
  for (const key of ['authorization', 'content-type', 'accept', 'x-api-key', 'anthropic-version']) {
    const value = source.get(key)
    if (value) forwarded.set(key, value)
  }
  forwarded.set('user-agent', 'wyckoff-agent/1.0')
  return forwarded
}

function isOneRouteChatCompletion(url: string): boolean {
  try {
    const target = new URL(url)
    return ONE_ROUTE_ORIGINS.has(target.origin) && target.pathname.endsWith('/chat/completions')
  } catch {
    return false
  }
}

function isGeminiChatCompletion(url: string): boolean {
  return url.startsWith('https://generativelanguage.googleapis.com') && url.includes('/chat/completions')
}

function isSseResponse(response: Response): boolean {
  return /\btext\/event-stream\b/i.test(response.headers.get('content-type') || '')
}

function patchOneRouteBody(body: BodyInit | null | undefined): BodyInit | null | undefined {
  if (typeof body !== 'string') return body
  try {
    const payload = JSON.parse(body) as Record<string, unknown>
    delete payload.stream_options
    return JSON.stringify(payload)
  } catch {
    return body
  }
}

function normalizeStreamError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  if (/Tool results? (are|is) missing for tool calls?/i.test(message)) {
    return '上一次工具调用被中断，请重新发送当前问题。'
  }
  const statusCode = providerStatusCode(error)
  if (statusCode === 503 || /Service temporarily unavailable/i.test(message)) {
    return '模型服务暂时不可用（上游 503）。请稍后重试，或在设置里切换到其他可用模型。'
  }
  if (statusCode === 401 || statusCode === 403) {
    return '模型服务鉴权失败，请检查设置页里的模型 API Key。'
  }
  if (statusCode === 404) {
    return '模型服务找不到当前模型，请检查设置页里的模型名称。'
  }
  return message
}

function providerStatusCode(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null
  const direct = Number((error as { statusCode?: unknown }).statusCode)
  if (Number.isFinite(direct)) return direct
  const lastError = (error as { lastError?: unknown }).lastError
  if (lastError && typeof lastError === 'object') {
    const nested = Number((lastError as { statusCode?: unknown }).statusCode)
    if (Number.isFinite(nested)) return nested
  }
  return null
}
