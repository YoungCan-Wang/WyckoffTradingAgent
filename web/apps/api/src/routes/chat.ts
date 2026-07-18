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
  execScreenStocks,
  execSearchStock,
  execStrategyDecision,
  execViewPortfolio,
  fetchMarketWatchSnapshot,
  formatMarketWatchContext,
  selectMarketWatchCodes,
  ALLOWED_PROXY_TARGET_ORIGINS,
  normalizeGeminiStream,
  PROVIDER_BASE_URLS,
  PROVIDER_DEFAULT_MODELS,
  isSafeProviderBaseUrl,
  isAllowedModelBaseUrl,
  type LLMToolConfig,
  type Provider,
  type ToolDeps,
  type MarketWatchSnapshot,
} from '@wyckoff/shared'
import { consumeStream, convertToModelMessages, createUIMessageStream, createUIMessageStreamResponse, generateText, stepCountIs, streamText, tool, type UIMessage, type UIMessageChunk } from 'ai'
import { Hono } from 'hono'
import { z } from 'zod'
import type { Env } from '../app'
import { authMiddleware, type AuthContext } from '../middleware/auth'
import { chatRateLimitMiddleware } from '../middleware/rate-limit'

type ChatBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const chatRoutes = new Hono<ChatBindings>()

chatRoutes.use('*', authMiddleware)

chatRoutes.get('/config', async (c) => {
  const auth = c.get('auth')
  const supabase = createUserSupabase(c.env, auth.accessToken)
  const config = await loadLLMConfig(supabase, auth.userId)
  return c.json({ configured: Boolean(config), model: config?.model || null })
})

chatRoutes.post('/', chatRateLimitMiddleware, async (c) => {
  const auth = c.get('auth')
  const body = await c.req.json<ChatRequestBody>().catch(() => null)
  const messages = body?.messages
  if (!Array.isArray(messages) || messages.length === 0) return c.json({ error: 'Missing messages' }, 400)
  if (estimateMessagesSize(messages) > 60_000) return c.json({ error: '本轮上下文过长，请开启新对话或缩短问题。' }, 413)

  const supabase = createUserSupabase(c.env, auth.accessToken)
  const configs = await loadLLMConfigs(supabase, auth.userId)
  if (configs.length === 0) return c.json({ error: '请先在设置页配置 LLM API Key' }, 400)
  const runId = crypto.randomUUID()

  const stream = createUIMessageStream({
    execute: ({ writer }) => runChatWithResilience({
      writer,
      configs,
      deps: createToolDeps(supabase),
      userId: auth.userId,
      messages,
      signal: c.req.raw.signal,
      env: c.env,
      runId,
      sequence: 0,
      watchlist: sanitizeWatchlist(body?.watchlist),
      marketWatchCache: body?.marketWatch,
    }),
    onError: normalizeStreamError,
  })
  return createUIMessageStreamResponse({ stream, consumeSseStream: consumeStream })
})

type ChatRequestBody = { messages?: UIMessage[]; watchlist?: unknown; marketWatch?: unknown }
type WatchlistRequestItem = { code: string; name: string }

const ALLOWED_TARGET_ORIGINS: Set<string> = new Set(ALLOWED_PROXY_TARGET_ORIGINS)
const ONE_ROUTE_ORIGINS = new Set(['https://api.1route.dev', 'https://www.1route.dev'])

const CHAT_MAX_OUTPUT_TOKENS = 8192
const CHAT_MAX_STEPS = 16

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

function estimateMessagesSize(messages: UIMessage[]): number {
  return messages.reduce((total, message) => total + JSON.stringify(message).length, 0)
}

function sanitizeWatchlist(value: unknown): WatchlistRequestItem[] {
  if (!Array.isArray(value)) return []
  const items = value.flatMap((item): WatchlistRequestItem[] => {
    if (typeof item === 'string') {
      const code = item.trim().toUpperCase()
      return code.length > 0 && code.length <= 24 ? [{ code, name: '' }] : []
    }
    if (!item || typeof item !== 'object' || Array.isArray(item)) return []
    const record = item as Record<string, unknown>
    const code = typeof record.code === 'string' ? record.code.trim().toUpperCase() : ''
    const name = typeof record.name === 'string' ? record.name.trim().slice(0, 80) : ''
    return code.length > 0 && code.length <= 24 ? [{ code, name }] : []
  })
  const seen = new Set<string>()
  return items.filter((item) => {
    if (seen.has(item.code)) return false
    seen.add(item.code)
    return true
  }).slice(0, 18)
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
    const response = await globalThis.fetch(input, { ...init, body: patchedBody, redirect: 'manual' })
    if (response.status >= 300 && response.status < 400) throw new Error('Model provider redirects are not allowed')
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

type ChatModelConfig = LLMToolConfig & { protocol?: 'openai' | 'anthropic'; provider: string }

async function loadLLMConfig(supabase: ToolDeps['supabase'], userId: string): Promise<ChatModelConfig | null> {
  return (await loadLLMConfigs(supabase, userId))[0] || null
}

async function loadLLMConfigs(supabase: ToolDeps['supabase'], userId: string): Promise<ChatModelConfig[]> {
  const { data } = await supabase
    .from('user_settings')
    .select('chat_provider, gemini_api_key, gemini_model, gemini_base_url, openai_api_key, openai_model, openai_base_url, deepseek_api_key, deepseek_model, deepseek_base_url, anthropic_api_key, anthropic_model, anthropic_base_url, custom_providers')
    .eq('user_id', userId)
    .single()
  if (!data) return []
  const settings = data as UserSettingsRow
  const activeProvider = String(settings.chat_provider || '1route')
  const custom = parseCustomProviders(settings.custom_providers)
  const providers = Array.from(new Set([activeProvider, 'openai', 'deepseek', 'gemini', 'anthropic', ...Object.keys(custom)]))
  return providers
    .filter((provider) => !['zhipu', 'minimax', 'qwen', 'volcengine'].includes(provider))
    .flatMap((provider) => {
      const config = configForProvider(settings, provider)
      return config ? [{ ...config, provider }] : []
    })
}

type UserSettingsRow = Record<string, string | Record<string, unknown> | null>

function configForProvider(data: UserSettingsRow, provider = String(data.chat_provider || '1route')): (LLMToolConfig & { protocol?: 'openai' | 'anthropic' }) | null {
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
  return api_key && model && isSafeProviderBaseUrl(base_url) && isAllowedModelBaseUrl(base_url) ? { api_key, model, base_url } : null
}

function customProviderConfig(data: UserSettingsRow, provider: string): LLMToolConfig | null {
  const custom = parseCustomProviders(data.custom_providers)
  const info = custom[provider] || {}
  const api_key = info.apikey || info.api_key || ''
  const model = info.model || defaultModelForProvider(provider)
  const base_url = info.baseurl || info.base_url || defaultBaseUrlForProvider(provider)
  return api_key && model && isSafeProviderBaseUrl(base_url) && isAllowedModelBaseUrl(base_url) ? { api_key, model, base_url } : null
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

interface ChatResilienceArgs {
  writer: { write: (chunk: never) => void }
  configs: ChatModelConfig[]
  deps: ToolDeps
  userId: string
  messages: UIMessage[]
  signal: AbortSignal
  env: Env
  runId: string
  sequence: number
  watchlist: WatchlistRequestItem[]
  marketWatchCache: unknown
}

async function runChatWithResilience(args: ChatResilienceArgs): Promise<void> {
  const selectedCodes = selectMarketWatchCodes(args.watchlist, recentUserQuery(args.messages))
  const marketWatch = await fetchMarketWatchSnapshot(args.deps, args.userId, selectedCodes, args.marketWatchCache)
  if (marketWatch.state !== 'empty') writeMarketWatchStatus(args.writer, marketWatch)
  const marketWatchContext = formatMarketWatchContext(marketWatch)
  let lastError: unknown = new Error('没有可用的模型配置')
  for (let index = 0; index < args.configs.length; index += 1) {
    const config = args.configs[index]
    if (!config) continue
    for (let attempt = 1; attempt <= 2; attempt += 1) {
      try {
        await runChatAttempt(args, config, marketWatchContext)
        return
      } catch (error) {
        lastError = error
        const started = Boolean((error as { outputStarted?: boolean }).outputStarted)
        const retryable = isRetryableModelError(error)
        const nextConfig = args.configs[index + 1]
        const canRetry = !started && retryable && attempt < 2
        const canFallback = !started && retryable && !canRetry && Boolean(nextConfig)
        if (args.signal.aborted || started || (!canRetry && !canFallback)) throw error
        writeModelStatus(args.writer, canRetry ? {
          phase: 'retrying', model: config.model, attempt,
        } : {
          phase: 'fallback', model: config.model, attempt, nextModel: nextConfig?.model,
        })
        await waitForRetry(attempt, args.signal)
        if (canFallback) break
      }
    }
  }
  throw lastError
}

function recentUserQuery(messages: UIMessage[]): string {
  return messages
    .filter((message) => message.role === 'user')
    .slice(-3)
    .map((message) => JSON.stringify(message))
    .join('\n')
}

async function runChatAttempt(args: ChatResilienceArgs, config: ChatModelConfig, marketWatchContext: string): Promise<void> {
  writeRunEvent(args, { type: 'model_started', label: `开始使用 ${config.model}` })
  writeStageProgress(args.writer, { stage: 'model', state: 'started', message: '正在分析', model: config.model })
  let succeeded = false
  let outputStarted = false
  try {
    const provider = createProvider(config)
    const tools = buildTools(args.deps, args.userId, config, provider.chat(config.model))
    const modelMessages = await convertToModelMessages(args.messages.slice(-40), {
      tools,
      ignoreIncompleteToolCalls: true,
    })
    const result = streamText({
      model: provider.chat(config.model),
      system: [WYCKOFF_CHAT_SYSTEM_PROMPT, marketWatchContext].filter(Boolean).join('\n\n'),
      messages: modelMessages,
      tools,
      maxOutputTokens: CHAT_MAX_OUTPUT_TOKENS,
      stopWhen: stepCountIs(CHAT_MAX_STEPS),
      abortSignal: args.signal,
      experimental_toolApprovalSecret: args.env.CHAT_TOOL_APPROVAL_SECRET || args.env.SUPABASE_SERVICE_ROLE_KEY,
      providerOptions: { openai: { parallelToolCalls: false } },
    })
    const pending: UIMessageChunk[] = []
    for await (const chunk of result.toUIMessageStream({ onError: (error) => { throw error } })) {
      if (chunk.type === 'error') throw new Error(chunk.errorText)
      writeChunkRunEvent(args, chunk)
      pending.push(chunk)
      if (isVisibleChatChunk(chunk)) {
        outputStarted = true
        flushChunks(args.writer, pending)
      }
    }
    const finishReason = await result.finishReason
    if (finishReason === 'length') {
      throw new Error(`模型输出达到 ${CHAT_MAX_OUTPUT_TOKENS} 个 token 上限，当前回答可能未完整结束。请点击“继续本轮”完成剩余分析。`)
    }
    flushChunks(args.writer, pending)
    succeeded = true
  } catch (error) {
    throw Object.assign(error instanceof Error ? error : new Error(String(error)), {
      outputStarted,
    })
  } finally {
    writeStageProgress(args.writer, { stage: 'model', state: 'completed', success: succeeded, model: config.model })
    writeRunEvent(args, { type: succeeded ? 'model_completed' : 'model_failed', label: succeeded ? '模型分析完成' : '模型分析失败' })
  }
}

function flushChunks(writer: ChatResilienceArgs['writer'], chunks: UIMessageChunk[]): void {
  while (chunks.length > 0) writer.write(chunks.shift() as never)
}

function isVisibleChatChunk(chunk: UIMessageChunk): boolean {
  return chunk.type === 'text-start' || chunk.type === 'text-delta' || chunk.type === 'reasoning-start' || chunk.type === 'tool-input-start' || chunk.type === 'tool-input-available' || chunk.type === 'tool-output-available' || chunk.type === 'tool-output-error' || chunk.type === 'tool-approval-request'
}

function writeModelStatus(writer: ChatResilienceArgs['writer'], status: { phase: 'retrying' | 'fallback'; model: string; attempt: number; nextModel?: string }): void {
  writer.write({ type: 'data-model-status', data: { kind: 'model', ...status }, transient: true } as never)
}

function writeStageProgress(
  writer: ChatResilienceArgs['writer'],
  progress: { stage: 'model'; state: 'started' | 'completed'; message?: string; success?: boolean; model: string },
): void {
  writer.write({ type: 'data-stage-progress', data: { kind: 'stage', ...progress }, transient: true } as never)
}

function writeMarketWatchStatus(writer: ChatResilienceArgs['writer'], snapshot: MarketWatchSnapshot): void {
  writer.write({ type: 'data-market-watch', data: snapshot, transient: true } as never)
}

function writeChunkRunEvent(args: ChatResilienceArgs, chunk: UIMessageChunk): void {
  if (chunk.type === 'tool-input-start') {
    writeRunEvent(args, { type: 'tool_started', label: `读取 ${chunk.toolName}`, toolName: chunk.toolName })
  } else if (chunk.type === 'tool-output-available') {
    writeRunEvent(args, { type: 'tool_completed', label: '数据读取完成', toolCallId: chunk.toolCallId })
  } else if (chunk.type === 'tool-output-error') {
    writeRunEvent(args, { type: 'tool_failed', label: '数据读取失败', toolCallId: chunk.toolCallId })
  } else if (chunk.type === 'text-start') {
    writeRunEvent(args, { type: 'answer_started', label: '开始生成结论' })
  }
}

function writeRunEvent(
  args: ChatResilienceArgs,
  event: { type: string; label: string; toolName?: string; toolCallId?: string },
): void {
  args.sequence += 1
  args.writer.write({
    type: 'data-run-event',
    data: {
      runId: args.runId,
      sequence: args.sequence,
      type: event.type,
      label: event.label,
      toolName: event.toolName,
      toolCallId: event.toolCallId,
      timestamp: new Date().toISOString(),
    },
    transient: true,
  } as never)
}

function isRetryableModelError(error: unknown): boolean {
  const status = providerStatusCode(error)
  if (status == null) return true
  return status === 408 || status === 409 || status === 429 || status >= 500
}

async function waitForRetry(attempt: number, signal: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, attempt * 350)
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason) }, { once: true })
  })
}

function buildReadTools(deps: ToolDeps, userId: string, model: unknown) {
  return {
    search_stock: tool({ description: '搜索股票，支持代码或名称。', inputSchema: z.object({ query: z.string() }), execute: ({ query }) => execSearchStock(deps, userId, query) }),
    view_portfolio: tool({ description: '查看用户当前持仓列表和可用资金。', inputSchema: z.object({}), execute: () => execViewPortfolio(deps, userId) }),
    market_overview: tool({ description: '查看当前/最新大盘行情信号。', inputSchema: z.object({}), execute: () => execMarketOverview(deps) }),
    market_history: tool({ description: '回看大盘指数过去N个交易日K线，分析量价关系和威科夫阶段。', inputSchema: z.object({ days: z.number().nullable(), index: z.enum(['sse', 'csi300', 'szse', 'chinext']).nullable() }), execute: ({ days, index }) => execMarketHistory(deps, userId, model, days ?? 100, index ?? 'sse') }),
    query_recommendations: tool({ description: '查询形态复盘记录。', inputSchema: z.object({ limit: z.number() }), execute: ({ limit }) => execQueryRecommendations(deps, limit) }),
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
  const value = error as { statusCode?: unknown; status?: unknown; response?: { status?: unknown } }
  for (const candidate of [value.statusCode, value.status, value.response?.status]) {
    const direct = Number(candidate)
    if (Number.isFinite(direct)) return direct
  }
  const lastError = (error as { lastError?: unknown }).lastError
  if (lastError && typeof lastError === 'object') {
    const nestedValue = lastError as { statusCode?: unknown; status?: unknown; response?: { status?: unknown } }
    for (const candidate of [nestedValue.statusCode, nestedValue.status, nestedValue.response?.status]) {
      const nested = Number(candidate)
      if (Number.isFinite(nested)) return nested
    }
  }
  return null
}
