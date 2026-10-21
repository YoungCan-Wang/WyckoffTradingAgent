/**
 * DeepSeek 独立 Search / Fetch。
 * 契约见 https://api-docs.deepseek.com/zh-cn/guides/search ：
 * POST /v1/tools/search 与 POST /v1/tools/fetch，Bearer 鉴权。
 * 只请求官方 origin，避免把 Key 送到代理域名；与 Responses 的 web_search 不是同一条路径。
 */
import { DEEPSEEK_OFFICIAL_ORIGIN, isOfficialDeepSeekBaseUrl } from './deepseek'

export const DEEPSEEK_WEB_SEARCH_PATH = '/v1/tools/search'
export const DEEPSEEK_WEB_FETCH_PATH = '/v1/tools/fetch'
export const DEEPSEEK_WEB_SEARCH_TIMEOUT_MS = 20_000
export const DEEPSEEK_WEB_FETCH_TIMEOUT_MS = 30_000
export const DEEPSEEK_WEB_QUERY_MAX_CHARS = 128
export const DEEPSEEK_WEB_FETCH_URL_MAX_CHARS = 8192
export const DEEPSEEK_WEB_FETCH_MAX_CHARS = 12_000
export const DEEPSEEK_WEB_PRICE_PER_THOUSAND_CNY = 5

export type DeepSeekWebFailure = {
  ok: false
  status: number | null
  code: string
  message: string
  retryable: boolean
}

export type DeepSeekSearchHit = {
  rerank_score: number | null
  title: string
  url: string
  snippet: string
  summary: string
  content: string
  site_name: string | null
  site_icon: string | null
  date: string | null
  authority: number | null
}

export type DeepSeekSearchSuccess = {
  ok: true
  results: DeepSeekSearchHit[]
  empty: boolean
}

export type DeepSeekFetchSuccess = {
  ok: true
  title: string
  content: string
  truncated: boolean
  empty: boolean
}

export type DeepSeekSearchResult = DeepSeekSearchSuccess | DeepSeekWebFailure
export type DeepSeekFetchResult = DeepSeekFetchSuccess | DeepSeekWebFailure

export type DeepSeekWebClientOptions = {
  apiKey: string
  baseUrl: string
  fetchImpl?: typeof globalThis.fetch
  signal?: AbortSignal
  searchTimeoutMs?: number
  fetchTimeoutMs?: number
}

type SearchRequest = DeepSeekWebClientOptions & {
  query: string
  count?: number
  oriQuestion?: string | null
  localization?: string | null
  needContent?: boolean
}

const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504])

export async function deepseekWebSearch(request: SearchRequest): Promise<DeepSeekSearchResult> {
  const payload = searchPayload(request)
  if (!payload.ok) return payload
  const response = await postTool(request, DEEPSEEK_WEB_SEARCH_PATH, payload.body, request.searchTimeoutMs ?? DEEPSEEK_WEB_SEARCH_TIMEOUT_MS)
  if (!response.ok) return response
  return parseSearchBody(response.body)
}

export async function deepseekWebFetch(request: DeepSeekWebClientOptions & { url: string }): Promise<DeepSeekFetchResult> {
  const payload = fetchPayload(request.url)
  if (!payload.ok) return payload
  const response = await postTool(request, DEEPSEEK_WEB_FETCH_PATH, payload.body, request.fetchTimeoutMs ?? DEEPSEEK_WEB_FETCH_TIMEOUT_MS)
  if (!response.ok) return response
  return parseFetchBody(response.body)
}

function searchPayload(request: SearchRequest): { ok: true; body: Record<string, unknown> } | DeepSeekWebFailure {
  const queryError = boundedText(request.query, DEEPSEEK_WEB_QUERY_MAX_CHARS, 'query')
  if (typeof queryError !== 'string') return queryError
  const body: Record<string, unknown> = { query: queryError }
  if (request.count != null) {
    if (!Number.isInteger(request.count) || request.count < 1 || request.count > 50) {
      return failure(null, 'invalid_request', 'count 必须是 1～50 的整数。', false)
    }
    body.count = request.count
  }
  const question = optionalBounded(request.oriQuestion, 'ori_question')
  if (question && !question.ok) return question
  if (question?.value) body.ori_question = question.value
  const localization = optionalIp(request.localization)
  if (localization && !localization.ok) return localization
  if (localization?.value) body.localization = localization.value
  if (request.needContent) body.need_content = true
  return { ok: true, body }
}

function fetchPayload(raw: string): { ok: true; body: Record<string, unknown> } | DeepSeekWebFailure {
  const url = validateFetchUrl(raw)
  if (!url.ok) return url
  return { ok: true, body: { url: url.url } }
}

function optionalBounded(value: string | null | undefined, field: string): { ok: true; value: string } | DeepSeekWebFailure | null {
  if (value == null || value === '') return null
  const text = boundedText(value, DEEPSEEK_WEB_QUERY_MAX_CHARS, field)
  return typeof text === 'string' ? { ok: true, value: text } : text
}

function optionalIp(value: string | null | undefined): { ok: true; value: string } | DeepSeekWebFailure | null {
  if (value == null || value.trim() === '') return null
  const ip = value.trim()
  if (!isIpAddress(ip)) return failure(null, 'invalid_request', 'localization 必须是 IP 地址。', false)
  return { ok: true, value: ip }
}

function boundedText(value: string, max: number, field: string): string | DeepSeekWebFailure {
  const text = value.trim()
  if (!text) return failure(null, 'invalid_request', `${field} 不能为空。`, false)
  if (Array.from(text).length > max) return failure(null, 'invalid_request', `${field} 不能超过 ${max} 个字符。`, false)
  return text
}

function validateFetchUrl(raw: string): { ok: true; url: string } | DeepSeekWebFailure {
  const url = raw.trim()
  if (Array.from(url).length > DEEPSEEK_WEB_FETCH_URL_MAX_CHARS) {
    return failure(null, 'invalid_request', 'url 不能超过 8192 个字符。', false)
  }
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return failure(null, 'invalid_request', 'url 必须是含 host 的 HTTP 或 HTTPS 地址。', false)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return failure(null, 'invalid_request', 'url 必须是 HTTP 或 HTTPS。', false)
  }
  if (!parsed.hostname || parsed.username || parsed.password) {
    return failure(null, 'invalid_request', 'url 必须含 host，且不能携带用户名或密码。', false)
  }
  return { ok: true, url }
}

async function postTool(
  options: DeepSeekWebClientOptions,
  path: string,
  body: Record<string, unknown>,
  timeoutMs: number,
): Promise<{ ok: true; body: unknown } | DeepSeekWebFailure> {
  if (!options.apiKey.trim()) return failure(null, 'unauthorized', '缺少 DeepSeek API Key。', false)
  if (!isOfficialDeepSeekBaseUrl(options.baseUrl)) {
    return failure(null, 'unconfigured', '独立 Search/Fetch 只接受官方 DeepSeek 地址。', false)
  }
  const abort = abortAfter(timeoutMs, options.signal)
  try {
    const response = await (options.fetchImpl ?? globalThis.fetch)(`${DEEPSEEK_OFFICIAL_ORIGIN}${path}`, {
      method: 'POST',
      redirect: 'manual',
      headers: {
        Authorization: `Bearer ${options.apiKey.trim()}`,
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify(body),
      signal: abort.signal,
    })
    return await readResponse(response)
  } catch (error) {
    return abortFailure(error, abort.timedOut(), options.signal)
  } finally {
    abort.done()
  }
}

function parseSearchBody(body: unknown): DeepSeekSearchResult {
  const results = asRecord(body)?.results
  if (!Array.isArray(results)) return failure(200, 'invalid_response', 'Search 响应缺少 results。', false)
  const hits = results.flatMap((item) => {
    const hit = parseHit(item)
    return hit ? [hit] : []
  })
  return { ok: true, results: hits, empty: hits.length === 0 }
}

function parseHit(value: unknown): DeepSeekSearchHit | null {
  const item = asRecord(value)
  if (!item || typeof item.url !== 'string' || !item.url) return null
  return {
    rerank_score: finiteNumber(item.rerank_score),
    title: clip(text(item.title), 500),
    url: item.url,
    snippet: clip(text(item.snippet), 1_000),
    summary: clip(text(item.summary), 4_000),
    content: clip(text(item.content), 8_000),
    site_name: nullableText(item.site_name),
    site_icon: nullableText(item.site_icon),
    date: nullableText(item.date),
    authority: finiteInt(item.authority),
  }
}

function parseFetchBody(body: unknown): DeepSeekFetchResult {
  const item = asRecord(body)
  if (!item || typeof item.content !== 'string' || typeof item.title !== 'string') {
    return failure(200, 'invalid_response', 'Fetch 响应缺少 title 或 content。', false)
  }
  const content = clip(item.content, DEEPSEEK_WEB_FETCH_MAX_CHARS)
  return {
    ok: true,
    title: item.title,
    content,
    truncated: content.length < item.content.length,
    empty: item.content.trim().length === 0,
  }
}

async function readResponse(response: Response): Promise<{ ok: true; body: unknown } | DeepSeekWebFailure> {
  if (response.status >= 300 && response.status < 400) {
    await response.body?.cancel()
    return failure(response.status, 'redirect', 'DeepSeek 网页工具拒绝跳转。', false)
  }
  const body = await response.json().catch(() => null)
  if (response.status === 200) {
    return body == null ? failure(200, 'invalid_response', 'DeepSeek 网页工具返回了无法解析的结果。', false) : { ok: true, body }
  }
  const upstream = errorParts(body)
  return failure(response.status, statusCode(response.status), upstream || statusMessage(response.status), RETRYABLE_STATUS.has(response.status) || response.status >= 500)
}

function abortFailure(error: unknown, timedOut: boolean, parent?: AbortSignal): DeepSeekWebFailure {
  if (timedOut) return failure(null, 'timeout', 'DeepSeek 网页工具请求超时。', true)
  if (parent?.aborted) return failure(null, 'aborted', '请求已取消。', false)
  const name = error instanceof Error ? error.name : ''
  if (name === 'TimeoutError' || name === 'AbortError') return failure(null, 'timeout', 'DeepSeek 网页工具请求超时。', true)
  return failure(null, 'network', 'DeepSeek 网页工具请求失败。', true)
}

function abortAfter(timeoutMs: number, parent?: AbortSignal): { signal: AbortSignal; done: () => void; timedOut: () => boolean } {
  const controller = new AbortController()
  let didTimeout = false
  const timer = setTimeout(() => {
    didTimeout = true
    controller.abort(new DOMException('The operation was aborted due to timeout', 'TimeoutError'))
  }, timeoutMs)
  const onParent = () => controller.abort(parent?.reason)
  if (parent?.aborted) onParent()
  else parent?.addEventListener('abort', onParent, { once: true })
  return {
    signal: controller.signal,
    timedOut: () => didTimeout,
    done: () => {
      clearTimeout(timer)
      parent?.removeEventListener('abort', onParent)
    },
  }
}

function failure(status: number | null, code: string, message: string, retryable: boolean): DeepSeekWebFailure {
  return { ok: false, status, code, message, retryable }
}

function statusCode(status: number): string {
  if (status === 400 || status === 413) return 'invalid_request'
  if (status === 401) return 'unauthorized'
  if (status === 402) return 'payment_required'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 408) return 'timeout'
  if (status === 429) return 'rate_limited'
  if (status >= 500) return 'upstream'
  return 'http_error'
}

function statusMessage(status: number): string {
  if (status === 400) return '搜索或抓取请求不合法。'
  if (status === 401) return 'DeepSeek API Key 缺失或无效。'
  if (status === 402) return 'DeepSeek 账号余额不足。'
  if (status === 403) return '抓取被安全策略拒绝。'
  if (status === 404) return '未能提取可用的网页正文。'
  if (status === 408 || status === 504) return 'DeepSeek 网页工具超时。'
  if (status === 413) return '请求体超过大小限制。'
  if (status === 429) return '超过 DeepSeek Search/Fetch 每秒 5 次的账号限速。'
  if (status >= 500) return 'DeepSeek 网页工具暂时不可用。'
  return `DeepSeek 网页工具返回 HTTP ${status}。`
}

function errorParts(body: unknown): string {
  const message = asRecord(asRecord(body)?.error)?.message
  return typeof message === 'string' ? clip(message, 300) : ''
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function nullableText(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function finiteInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) ? value : null
}

function clip(value: string, max: number): string {
  const chars = Array.from(value)
  return chars.length <= max ? value : chars.slice(0, max).join('')
}

function isIpAddress(value: string): boolean {
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(value)) return value.split('.').every((part) => Number(part) <= 255)
  return value.includes(':') && value.length <= 45 && /^[0-9a-fA-F:]+$/.test(value)
}
