import { z } from 'zod'
import { apiUrl } from './api-url'

const navPointSchema = z.object({
  asOf: z.string(),
  nav: z.coerce.number(),
})

const showcaseSchema = z.object({
  navCurve: z.array(navPointSchema),
  periodPnlAmount: z.coerce.number(),
  periodPnlPct: z.coerce.number(),
  maxDrawdownPct: z.coerce.number(),
  winRatePct: z.number().nullable(),
  winRateNote: z.string().nullable(),
  openPositionCount: z.coerce.number(),
  sectorTags: z.array(z.string()),
})

const ledgerSchema = z.object({
  navDaily: z.array(z.object({
    asOf: z.string(),
    cash: z.number(),
    marketValue: z.number(),
    equity: z.number(),
    pnlDay: z.number(),
    pnlTotal: z.number(),
  })),
  events: z.array(z.object({
    asOf: z.string(),
    code: z.string(),
    name: z.string(),
    eventType: z.string(),
    price: z.number().nullable(),
    qty: z.number(),
    reason: z.string(),
    fees: z.record(z.number()),
  })),
  positions: z.array(z.object({
    code: z.string(),
    name: z.string(),
    shares: z.number(),
    avgCost: z.number(),
    lastMark: z.number(),
    netPnlAmount: z.number(),
    netPnlPct: z.number().nullable(),
  })),
})

const payloadSchema = z.object({
  tier: z.enum(['showcase', 'full']),
  accountId: z.string(),
  asOf: z.string().nullable(),
  disclaimer: z.string(),
  showcase: showcaseSchema,
  ledger: ledgerSchema.optional(),
})

const errorSchema = z.object({ error: z.string() })

export type ShadowLedgerPayload = z.infer<typeof payloadSchema>
export type ShadowLedgerShowcase = z.infer<typeof showcaseSchema>
export type ShadowNavDailyRow = z.infer<typeof ledgerSchema>['navDaily'][number]
export type ShadowEventRow = z.infer<typeof ledgerSchema>['events'][number]
export type ShadowPositionRow = z.infer<typeof ledgerSchema>['positions'][number]
export type ShadowRpcLoader = (asOf?: string) => Promise<unknown | null>

export function shadowLedgerErrorMessage(payload: unknown, status: number): string {
  const parsed = errorSchema.safeParse(payload)
  const raw = parsed.success ? parsed.data.error : ''
  if (status === 404 || status === 503 || raw === 'Not Found' || raw.includes('未配置')) {
    return '影子账户接口尚未就绪，请登录后重试。这不是实盘。'
  }
  return raw || '影子账本请求失败'
}

export function parseShadowLedgerPayload(payload: unknown): ShadowLedgerPayload {
  const parsed = payloadSchema.safeParse(payload)
  if (!parsed.success) throw new Error('影子账本返回数据不完整，请稍后重试')
  if (parsed.data.tier === 'full' && !parsed.data.ledger) {
    throw new Error('影子账本返回数据不完整，请稍后重试')
  }
  return parsed.data
}

export async function loadShadowLedgerViaRpc(asOf?: string): Promise<unknown | null> {
  const { supabase } = await import('@/lib/supabase')
  const { data, error } = await supabase.rpc('shadow_ledger_payload', { p_as_of: asOf ?? null })
  return error ? null : data
}

export async function requestShadowLedger(
  accessToken?: string,
  asOf?: string,
  fetcher: typeof fetch = fetch,
  rpcLoader: ShadowRpcLoader = loadShadowLedgerViaRpc,
): Promise<ShadowLedgerPayload> {
  const query = asOf ? `?asOf=${encodeURIComponent(asOf)}` : ''
  const response = await fetcher(apiUrl(`/api/shadow-ledger${query}` as `/api/${string}`), {
    headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
  })
  const payload = await response.json().catch(() => null)
  if (response.ok) return parseShadowLedgerPayload(payload)
  if (accessToken && (response.status === 404 || response.status === 503)) {
    const fallback = await rpcLoader(asOf)
    if (fallback) return parseShadowLedgerPayload(fallback)
  }
  throw new Error(shadowLedgerErrorMessage(payload, response.status))
}
