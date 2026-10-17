import { tool, type ToolSet } from 'ai'
import { z } from 'zod'
import { isOfficialDeepSeekBaseUrl } from './deepseek'
import {
  DEEPSEEK_WEB_PRICE_PER_THOUSAND_CNY,
  deepseekWebFetch,
  deepseekWebSearch,
  type DeepSeekSearchHit,
  type DeepSeekWebFailure,
} from './deepseek-web'

/** 工具侧默认条数。API 允许 1～50，这里压到 10，避免把上下文撑满。 */
export const DEEPSEEK_WEB_SEARCH_DEFAULT_COUNT = 5
export const DEEPSEEK_WEB_SEARCH_TOOL_MAX_COUNT = 10
/**
 * Fetch 单独开启：成功抓取按次计费，所以不在 Search 里打开 need_content。
 * 模型只有在摘要不够、且只核一条 URL 时才应调用。
 */
export const DEEPSEEK_STANDALONE_FETCH_ENABLED = true

const PRICE_NOTE = `成功且有内容的请求约 ${DEEPSEEK_WEB_PRICE_PER_THOUSAND_CNY} 元/千次；失败、空结果和超时不计费`

export const DEEPSEEK_STANDALONE_WEB_GUIDANCE = `# 独立网页搜索与抓取

当前模型没有 DeepSeek Responses 的服务端 web_search。用户已配置官方 DeepSeek Key 时，可以调用 deepseek_web_search 与 deepseek_web_fetch。
公开网页、舆情、宏观或本地库查不到时，先调用 deepseek_web_search。A股个股消息仍优先 stock_news。
deepseek_web_fetch ${PRICE_NOTE}，只抓一条必须核对的 URL，不要把搜索结果逐条抓取。
行情、持仓、形态复盘和归因仍必须用本地工具，不得用网页内容替代 K 线。搜索证据仅当轮有效。`

export type DeepSeekWebCredentialConfig = {
  provider: string
  api_key: string
  base_url: string
}

export type DeepSeekWebCredential = {
  apiKey: string
  baseUrl: string
}

type SearchArgs = {
  query: string
  count: number | null
  ori_question: string | null
  localization: string | null
}

/**
 * Responses web_search 已经会搜一次。再挂独立 Search 会让同一轮打两次付费检索。
 * 代理域名上的 Key 也不能拿去请求 api.deepseek.com。
 */
export function officialDeepSeekWebCredential(configs: DeepSeekWebCredentialConfig[]): DeepSeekWebCredential | null {
  const match = configs.find((config) => config.provider === 'deepseek' && config.api_key.trim() && isOfficialDeepSeekBaseUrl(config.base_url))
  return match ? { apiKey: match.api_key, baseUrl: match.base_url } : null
}

export function standaloneDeepSeekWebEnabled(responsesWebSearch: boolean, credential: DeepSeekWebCredential | null): boolean {
  return !responsesWebSearch && credential != null
}

export function buildDeepSeekWebTools(
  credential: DeepSeekWebCredential,
  fetchImpl: typeof globalThis.fetch = globalThis.fetch,
): ToolSet {
  const tools: ToolSet = {
    deepseek_web_search: tool({
      description: `用 DeepSeek 独立 Search API 搜索公开网页，返回标题、链接、短摘要和给模型读的长摘要。不替代行情、持仓或 K 线。A股个股消息优先 stock_news。query 最多 128 字，可用 site:、-site:、引号、lang:、after:、before:。默认 ${DEEPSEEK_WEB_SEARCH_DEFAULT_COUNT} 条，最多 ${DEEPSEEK_WEB_SEARCH_TOOL_MAX_COUNT} 条，不返回网页正文。${PRICE_NOTE}。需要整页时再调用 deepseek_web_fetch。`,
      inputSchema: z.object({
        query: z.string().describe('搜索词，1～128 个字符。'),
        count: z.number().int().nullable().describe('返回条数，1～10；不需要时传 null。'),
        ori_question: z.string().nullable().describe('用户原问题，1～128 字；没有时传 null。'),
        localization: z.string().nullable().describe('仅当用户明确要求按公网 IP 本地化时传入，否则 null。'),
      }),
      execute: (args) => runDeepSeekWebSearch(credential, fetchImpl, args),
    }),
  }
  if (DEEPSEEK_STANDALONE_FETCH_ENABLED) tools.deepseek_web_fetch = fetchTool(credential, fetchImpl)
  return tools
}

function fetchTool(credential: DeepSeekWebCredential, fetchImpl: typeof globalThis.fetch) {
  return tool({
    description: `用 DeepSeek 独立 Fetch API 把单个 http(s) 网页转成 Markdown。只在 deepseek_web_search 的摘要不够、且必须核对某一条 URL 时调用。不要批量抓取，不要抓登录页。${PRICE_NOTE}。`,
    inputSchema: z.object({
      url: z.string().describe('要抓取的单个 http(s) URL，不能带用户名或密码。'),
    }),
    execute: ({ url }) => runDeepSeekWebFetch(credential, fetchImpl, url),
  })
}

async function runDeepSeekWebSearch(
  credential: DeepSeekWebCredential,
  fetchImpl: typeof globalThis.fetch,
  args: SearchArgs,
): Promise<unknown> {
  const count = clampSearchCount(args.count)
  const result = await deepseekWebSearch({
    ...credential,
    fetchImpl,
    query: args.query,
    count,
    oriQuestion: args.ori_question,
    localization: args.localization,
  })
  if (!result.ok) return toolError(result)
  return {
    query: args.query.trim(),
    count,
    empty: result.empty,
    billed: !result.empty,
    results: result.results.map(presentHit),
    ...(result.empty ? { note: '没有匹配结果，本次不计费。' } : {}),
  }
}

async function runDeepSeekWebFetch(
  credential: DeepSeekWebCredential,
  fetchImpl: typeof globalThis.fetch,
  url: string,
): Promise<unknown> {
  const result = await deepseekWebFetch({ ...credential, fetchImpl, url })
  if (!result.ok) return toolError(result)
  return {
    url: url.trim(),
    title: result.title,
    content: result.content,
    truncated: result.truncated,
    empty: result.empty,
    billed: !result.empty,
    ...(result.empty ? { note: '没有提取到网页正文，本次不计费。' } : {}),
    ...(result.truncated ? { note: '正文已截断。请改抓更具体的 URL。' } : {}),
  }
}

function presentHit(hit: DeepSeekSearchHit): Record<string, unknown> {
  return {
    title: hit.title,
    url: hit.url,
    snippet: hit.snippet,
    summary: hit.summary,
    site_name: hit.site_name,
    date: hit.date,
    rerank_score: hit.rerank_score,
    authority: hit.authority,
    ...(hit.content ? { content: hit.content } : {}),
  }
}

function clampSearchCount(count: number | null): number {
  if (count == null || !Number.isInteger(count)) return DEEPSEEK_WEB_SEARCH_DEFAULT_COUNT
  return Math.min(DEEPSEEK_WEB_SEARCH_TOOL_MAX_COUNT, Math.max(1, count))
}

function toolError(error: DeepSeekWebFailure): Record<string, unknown> {
  return { error: error.message, status: error.status, code: error.code, retryable: error.retryable }
}
