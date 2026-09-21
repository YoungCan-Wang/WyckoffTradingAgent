import { z } from 'zod'
import { apiUrl } from './api-url'

const navPointSchema = z.object({
  asOf: z.string(),
  nav: z.number(),
})

const showcaseSchema = z.object({
  navCurve: z.array(navPointSchema),
  periodPnlAmount: z.number(),
  periodPnlPct: z.number(),
  maxDrawdownPct: z.number(),
  winRatePct: z.number().nullable(),
  winRateNote: z.string().nullable(),
  openPositionCount: z.number(),
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

export async function requestShadowLedger(
  accessToken?: string,
  asOf?: string,
  fetcher: typeof fetch = fetch,
): Promise<ShadowLedgerPayload> {
  const query = asOf ? `?asOf=${encodeURIComponent(asOf)}` : ''
  const response = await fetcher(apiUrl(`/api/shadow-ledger${query}` as `/api/${string}`), {
    headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const message = errorSchema.safeParse(payload)
    throw new Error(message.success ? message.data.error : '影子账本请求失败')
  }
  const parsed = payloadSchema.safeParse(payload)
  if (!parsed.success) throw new Error('影子账本返回数据不完整，请稍后重试')
  if (parsed.data.tier === 'full' && !parsed.data.ledger) {
    throw new Error('影子账本返回数据不完整，请稍后重试')
  }
  return parsed.data
}
