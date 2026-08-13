import { Hono } from 'hono'
import { z } from 'zod'
import { normalizePortfolioCode, refreshPortfolioTotalEquity } from '@wyckoff/shared'
import { authMiddleware, createUserSupabase, type AuthContext } from '../middleware/auth'
import { isActiveWhitelistUser } from '../middleware/whitelist'
import type { Env } from '../app'

type PortfolioBindings = { Bindings: Env; Variables: { auth: AuthContext } }

export const MISSING_BUY_DT_ERROR = '缺少建仓日 buy_dt，请询问用户后再写入'

export function normalizeBuyDate(value: unknown): unknown {
  if (value === '' || value == null) return null
  if (typeof value !== 'string') return value
  const text = value.trim()
  return /^\d{8}$/.test(text) ? `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6)}` : text
}

const POSITION_SCHEMA = z.object({
  code: z.string().trim().min(1).max(24),
  name: z.string().trim().max(80).nullable(),
  shares: z.number().int().positive().finite(),
  cost_price: z.number().positive().finite(),
  buy_dt: z.preprocess(
    normalizeBuyDate,
    z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
  ),
})

const PORTFOLIO_SCHEMA = z.object({
  free_cash: z.number().min(0).finite(),
  positions: z.array(POSITION_SCHEMA).max(100),
})

export const portfolioRoutes = new Hono<PortfolioBindings>()

portfolioRoutes.use('*', authMiddleware)

portfolioRoutes.get('/', async (c) => {
  const auth = c.get('auth')
  const supabase = createUserSupabase(c.env, auth.accessToken)
  if (!(await isActiveWhitelistUser(supabase, auth.userId))) return c.json({ error: 'Whitelist required' }, 403)
  const result = await loadPortfolio(supabase, auth.userId)
  return result.error ? c.json({ error: result.error }, 500) : c.json(result.portfolio)
})

portfolioRoutes.put('/', async (c) => {
  const auth = c.get('auth')
  const body = parsePortfolioInput(await c.req.json().catch(() => null))
  if ('error' in body) return c.json(body, 400)

  const supabase = createUserSupabase(c.env, auth.accessToken)
  if (!(await isActiveWhitelistUser(supabase, auth.userId))) return c.json({ error: 'Whitelist required' }, 403)
  const error = await savePortfolio(supabase, auth.userId, body.data)
  if (error) return c.json({ error }, 500)
  const valuation = await refreshPortfolioTotalEquity(
    { supabase, fetch: globalThis.fetch },
    auth.userId,
    {
      quoteEndpoint: `${(c.env.TICKFLOW_API_BASE || 'https://api.tickflow.org').replace(/\/$/, '')}/v1/quotes`,
      cnyRates: {
        HKD: positiveRate(c.env.PORTFOLIO_HKD_CNY_RATE),
        USD: positiveRate(c.env.PORTFOLIO_USD_CNY_RATE),
      },
    },
  )
  const result = await loadPortfolio(supabase, auth.userId)
  if (result.error) return c.json({ error: result.error }, 500)
  return c.json({
    ...result.portfolio,
    ...(valuation.ok ? {} : { valuation_warning: valuation.message }),
  })
})

export function parsePortfolioInput(raw: unknown):
  | { data: z.infer<typeof PORTFOLIO_SCHEMA> }
  | { error: string; details?: unknown } {
  const parsed = PORTFOLIO_SCHEMA.safeParse(raw)
  if (!parsed.success) return { error: 'Invalid portfolio', details: parsed.error.flatten() }
  const positions = []
  for (const item of parsed.data.positions) {
    const code = normalizePortfolioCode(item.code)
    if (!code) {
      return {
        error: `Invalid position code: ${item.code}`,
        details: 'Use A-share 6 digits, HK like 00700.HK, or US like AAPL.US',
      }
    }
    positions.push({ ...item, code })
  }
  const codes = positions.map((item) => item.code)
  if (new Set(codes).size !== codes.length) return { error: 'Duplicate position code' }
  return { data: { ...parsed.data, positions } }
}

async function loadPortfolio(supabase: ReturnType<typeof createUserSupabase>, userId: string) {
  const portfolioId = `USER_LIVE:${userId}`
  const [portfolioResult, positionsResult] = await Promise.all([
    supabase.from('portfolios').select('free_cash, total_equity, updated_at').eq('portfolio_id', portfolioId).maybeSingle(),
    supabase.from('portfolio_positions').select('code, name, shares, cost_price, buy_dt').eq('portfolio_id', portfolioId).order('buy_dt', { ascending: false }),
  ])
  const positions = z.array(POSITION_SCHEMA).safeParse(positionsResult.data || [])
  const error = portfolioResult.error?.message || positionsResult.error?.message ||
    (positions.success ? '' : 'Stored portfolio data is invalid')
  return {
    error,
    portfolio: {
      free_cash: Number(portfolioResult.data?.free_cash || 0),
      total_equity: portfolioResult.data?.total_equity == null ? null : Number(portfolioResult.data.total_equity),
      valuation_updated_at: portfolioResult.data?.updated_at || null,
      positions: positions.success ? positions.data : [],
    },
  }
}

function positiveRate(raw: string | undefined): number | undefined {
  const value = Number(raw || 0)
  return Number.isFinite(value) && value > 0 ? value : undefined
}

async function savePortfolio(
  supabase: ReturnType<typeof createUserSupabase>,
  userId: string,
  portfolio: z.infer<typeof PORTFOLIO_SCHEMA>,
): Promise<string> {
  const portfolioId = `USER_LIVE:${userId}`
  const cashError = await saveFreeCash(supabase, portfolioId, portfolio.free_cash)
  if (cashError) return cashError

  const { data: existing, error: readError } = await supabase
    .from('portfolio_positions')
    .select('code')
    .eq('portfolio_id', portfolioId)
  if (readError) return readError.message
  const wanted = new Set(portfolio.positions.map((item) => item.code.toUpperCase()))
  const removed = (existing || []).map((item) => String(item.code)).filter((code) => !wanted.has(code.toUpperCase()))
  const deleteError = await deleteRemovedPositions(supabase, portfolioId, removed)
  if (deleteError) return deleteError

  for (const position of portfolio.positions) {
    const error = await savePosition(supabase, portfolioId, { ...position, code: position.code.toUpperCase() })
    if (error) return error
  }
  return ''
}

async function saveFreeCash(
  supabase: ReturnType<typeof createUserSupabase>,
  portfolioId: string,
  freeCash: number,
): Promise<string> {
  const updated = await supabase.from('portfolios').update({ free_cash: freeCash }).eq('portfolio_id', portfolioId).select('portfolio_id')
  if (updated.error) return updated.error.message
  if ((updated.data || []).length > 0) return ''
  const inserted = await supabase.from('portfolios').insert({ portfolio_id: portfolioId, free_cash: freeCash })
  return inserted.error?.message || ''
}

async function deleteRemovedPositions(
  supabase: ReturnType<typeof createUserSupabase>,
  portfolioId: string,
  codes: string[],
): Promise<string> {
  for (const code of codes) {
    const result = await supabase.from('portfolio_positions').delete().eq('portfolio_id', portfolioId).eq('code', code)
    if (result.error) return result.error.message
  }
  return ''
}

async function savePosition(
  supabase: ReturnType<typeof createUserSupabase>,
  portfolioId: string,
  position: z.infer<typeof POSITION_SCHEMA>,
): Promise<string> {
  const record = positionWriteRecord(portfolioId, position)
  const updated = await supabase
    .from('portfolio_positions')
    .update(record)
    .eq('portfolio_id', portfolioId)
    .eq('code', position.code)
    .select('code')
  if (updated.error) return updated.error.message
  if ((updated.data || []).length > 0) return ''
  if (!record.buy_dt) return MISSING_BUY_DT_ERROR
  const inserted = await supabase.from('portfolio_positions').insert(record)
  return inserted.error?.message || ''
}

export function positionWriteRecord(
  portfolioId: string,
  position: z.infer<typeof POSITION_SCHEMA>,
): Record<string, unknown> {
  const buyDt = (position.buy_dt || '').trim()
  const record: Record<string, unknown> = {
    code: position.code,
    name: position.name || position.code,
    shares: position.shares,
    cost_price: position.cost_price,
    portfolio_id: portfolioId,
  }
  if (buyDt) record.buy_dt = buyDt
  return record
}
