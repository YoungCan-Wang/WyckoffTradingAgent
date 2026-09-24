export const SHADOW_ACCOUNT_ID = 'USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928'
export const SHADOW_DISCLAIMER = '策略按威科夫漏斗跑纸面账；非投资建议'

export const SHADOW_TABLES = [
  'shadow_account',
  'shadow_positions',
  'shadow_events',
  'shadow_nav_daily',
] as const

export const SHADOW_FORBIDDEN_TABLES = [
  'portfolios',
  'portfolio_positions',
  'trade_orders',
  'daily_nav',
] as const

export const SHADOW_SHOWCASE_FORBIDDEN_KEYS = [
  'code',
  'name',
  'price',
  'qty',
  'stop',
  'stop_loss',
  'stopLoss',
  'shares',
  'avg_cost',
  'avgCost',
  'last_mark',
  'lastMark',
  'entry_price',
  'entryPrice',
  'suggested_price',
  'stop_price',
  'reason',
  'fees',
  'eventType',
  'event_type',
] as const

const SECTOR_RULES: readonly [string, readonly string[]][] = [
  ['化肥', ['化肥', '磷肥', '钾肥', '氮肥', '复合肥']],
  ['化工', ['化工', '化学', '石化', '氯碱']],
  ['传媒', ['传媒', '影视', '广告', '出版', '游戏', '动漫']],
  ['银行', ['银行']],
  ['证券', ['证券', '券商']],
  ['保险', ['保险']],
  ['半导体', ['半导体', '芯片', '集成电路', '硅片']],
  ['新能源', ['新能源', '光伏', '锂电', '电池', '储能']],
  ['医药', ['医药', '生物', '制药', '医疗']],
  ['白酒', ['白酒']],
  ['食品', ['食品', '饮料', '乳业']],
  ['汽车', ['汽车', '整车', '汽配']],
  ['煤炭', ['煤炭', '焦煤']],
  ['钢铁', ['钢铁', '特钢']],
  ['有色', ['有色']],
  ['地产', ['地产', '房地产']],
  ['电力', ['电力', '电网', '火电', '水电']],
  ['军工', ['军工', '航空', '航天']],
  ['计算机', ['计算机', '软件', '互联网']],
  ['农业', ['农业', '种业', '饲料']],
  ['电子', ['电子', '光电']],
  ['机械', ['机械', '装备']],
]

export type ShadowLedgerTier = 'showcase' | 'full'

export type ShadowNavPoint = { asOf: string; nav: number }

export type ShadowShowcase = {
  navCurve: ShadowNavPoint[]
  periodPnlAmount: number
  periodPnlPct: number
  maxDrawdownPct: number
  winRatePct: number | null
  winRateNote: string | null
  openPositionCount: number
  sectorTags: string[]
}

export type ShadowNavDailyRow = {
  asOf: string
  cash: number
  marketValue: number
  equity: number
  pnlDay: number
  pnlTotal: number
}

export type ShadowEventRow = {
  asOf: string
  code: string
  name: string
  eventType: string
  price: number | null
  qty: number
  reason: string
  fees: Record<string, number>
}

export type ShadowPositionRow = {
  code: string
  name: string
  shares: number
  avgCost: number
  lastMark: number
  netPnlAmount: number
  netPnlPct: number | null
}

export type ShadowLedgerPayload = {
  tier: ShadowLedgerTier
  accountId: string
  asOf: string | null
  disclaimer: string
  showcase: ShadowShowcase
  ledger?: {
    navDaily: ShadowNavDailyRow[]
    events: ShadowEventRow[]
    positions: ShadowPositionRow[]
  }
}

export type ShadowAccountRow = {
  account_id?: string
  cash?: number | string
  equity?: number | string
  market_value?: number | string
  initial_capital?: number | string
  as_of?: string | null
}

export type ShadowNavSourceRow = {
  as_of?: string
  cash?: number | string
  market_value?: number | string
  equity?: number | string
  pnl_day?: number | string
  pnl_total?: number | string
}

export type ShadowPositionSourceRow = {
  code?: string
  name?: string
  shares?: number | string
  avg_cost?: number | string
  last_mark?: number | string | null
}

export type ShadowEventSourceRow = {
  as_of?: string
  code?: string
  name?: string
  event_type?: string
  price?: number | string | null
  qty?: number | string
  reason?: string
  fees?: unknown
  payload?: unknown
}

export type ShadowSnapshot = {
  account: ShadowAccountRow
  navDaily: ShadowNavSourceRow[]
  positions: ShadowPositionSourceRow[]
  events: ShadowEventSourceRow[]
}

type ShadowQuery = {
  select: (columns: string) => ShadowQuery
  eq: (column: string, value: string) => ShadowQuery
  gt: (column: string, value: number) => ShadowQuery
  order: (column: string, options?: { ascending?: boolean }) => ShadowQuery
  maybeSingle: () => Promise<{ data: ShadowAccountRow | null; error: { message: string } | null }>
  then: (
    resolve: (value: { data: unknown[] | null; error: { message: string } | null }) => unknown,
    reject?: (reason: unknown) => unknown,
  ) => Promise<unknown>
}

export type ShadowAdminClient = {
  from: (table: string) => ShadowQuery
}

export function assertShadowAccount(accountId: string): string {
  const text = accountId.trim()
  if (!text.startsWith('USER_SHADOW:') || text.startsWith('USER_LIVE')) {
    throw new Error('影子账本拒绝非 USER_SHADOW 账户')
  }
  return text
}

export function shadowTable(client: ShadowAdminClient, table: string): ShadowQuery {
  if ((SHADOW_FORBIDDEN_TABLES as readonly string[]).includes(table)) {
    throw new Error(`影子账本禁止访问表 ${table}`)
  }
  if (!(SHADOW_TABLES as readonly string[]).includes(table)) {
    throw new Error(`影子账本禁止访问表 ${table}`)
  }
  return client.from(table)
}

export function sectorTagFromName(name: string): string {
  const text = name.trim()
  for (const [tag, keywords] of SECTOR_RULES) {
    if (keywords.some((keyword) => text.includes(keyword))) return tag
  }
  return '其他'
}

export function openPositionNetPnl(shares: number, avgCost: number, lastMark: number) {
  const costBasis = shares * avgCost
  const amount = shares * lastMark - costBasis
  return { amount, pct: costBasis > 0 ? (amount / costBasis) * 100 : null }
}

export function maxDrawdownPct(equities: number[]): number {
  let peak = Number.NEGATIVE_INFINITY
  let worst = 0
  for (const equity of equities) {
    if (!Number.isFinite(equity)) continue
    if (equity > peak) peak = equity
    if (peak > 0) worst = Math.min(worst, (equity - peak) / peak)
  }
  return Math.abs(worst) * 100
}

export function crudeWinRate(events: ShadowEventSourceRow[]): { pct: number | null; note: string | null } {
  const closed = realizedClosePnls(events)
  if (closed.length === 0) return { pct: null, note: '平仓样本不足，未计算粗胜率' }
  const wins = closed.filter((pnl) => pnl > 0).length
  return { pct: (wins / closed.length) * 100, note: null }
}

export function buildShadowLedgerPayload(
  snapshot: ShadowSnapshot,
  tier: ShadowLedgerTier,
  asOfFilter?: string,
): ShadowLedgerPayload {
  const accountId = assertShadowAccount(String(snapshot.account.account_id || SHADOW_ACCOUNT_ID))
  const initialCapital = num(snapshot.account.initial_capital, 100_000)
  const navDaily = [...snapshot.navDaily]
    .map(toNavDaily)
    .filter((row) => row.asOf)
    .sort((a, b) => a.asOf.localeCompare(b.asOf))
  const positions = snapshot.positions
    .map(toPosition)
    .filter((row) => row.shares > 0)
    .sort((a, b) => a.code.localeCompare(b.code))
  const events = snapshot.events
    .map(toEvent)
    .filter((row) => row.asOf)
    .sort((a, b) => `${a.asOf}:${a.code}`.localeCompare(`${b.asOf}:${b.code}`))
  const last = navDaily[navDaily.length - 1]
  const pnlAmount = last ? last.pnlTotal : num(snapshot.account.equity, initialCapital) - initialCapital
  const winRate = crudeWinRate(snapshot.events)
  const payload: ShadowLedgerPayload = {
    tier,
    accountId,
    asOf: last?.asOf || snapshot.account.as_of || null,
    disclaimer: SHADOW_DISCLAIMER,
    showcase: {
      navCurve: navDaily.map((row) => ({
        asOf: row.asOf,
        nav: initialCapital > 0 ? row.equity / initialCapital : 0,
      })),
      periodPnlAmount: pnlAmount,
      periodPnlPct: initialCapital > 0 ? (pnlAmount / initialCapital) * 100 : 0,
      maxDrawdownPct: maxDrawdownPct(navDaily.map((row) => row.equity)),
      winRatePct: winRate.pct,
      winRateNote: winRate.note,
      openPositionCount: positions.length,
      sectorTags: uniqueSectorTags(positions),
    },
  }
  if (tier !== 'full') return payload
  const day = asOfFilter?.trim()
  payload.ledger = {
    navDaily,
    events: day ? events.filter((row) => row.asOf === day) : events,
    positions,
  }
  return payload
}

export function showcaseLeaksSensitiveFields(payload: ShadowLedgerPayload): string[] {
  return [...collectKeys(payload.showcase)].filter((key) =>
    (SHADOW_SHOWCASE_FORBIDDEN_KEYS as readonly string[]).includes(key),
  )
}

export async function loadShadowSnapshot(client: ShadowAdminClient): Promise<ShadowSnapshot> {
  const accountId = assertShadowAccount(SHADOW_ACCOUNT_ID)
  const [accountResult, navResult, positionResult, eventResult] = await Promise.all([
    shadowTable(client, 'shadow_account').select(
      'account_id,cash,equity,market_value,initial_capital,as_of',
    ).eq('account_id', accountId).maybeSingle(),
    Promise.resolve(shadowTable(client, 'shadow_nav_daily').select(
      'as_of,cash,market_value,equity,pnl_day,pnl_total',
    ).eq('account_id', accountId).order('as_of', { ascending: true })),
    Promise.resolve(shadowTable(client, 'shadow_positions').select(
      'code,name,shares,avg_cost,last_mark',
    ).eq('account_id', accountId).gt('shares', 0)),
    Promise.resolve(shadowTable(client, 'shadow_events').select(
      'as_of,code,name,event_type,price,qty,reason,fees,payload',
    ).eq('account_id', accountId).order('as_of', { ascending: true })),
  ])
  if (accountResult.error) throw new Error(accountResult.error.message)
  const account = accountResult.data || { account_id: accountId, initial_capital: 100_000 }
  assertShadowAccount(String(account.account_id || accountId))
  return {
    account,
    navDaily: rows(navResult),
    positions: rows(positionResult),
    events: rows(eventResult),
  }
}

function uniqueSectorTags(positions: ShadowPositionRow[]): string[] {
  const tags = new Set(positions.map((row) => sectorTagFromName(row.name)))
  return [...tags].sort((a, b) => (a === '其他' ? 1 : b === '其他' ? -1 : a.localeCompare(b, 'zh-CN')))
}

function toNavDaily(row: ShadowNavSourceRow): ShadowNavDailyRow {
  return {
    asOf: String(row.as_of || '').slice(0, 10),
    cash: num(row.cash),
    marketValue: num(row.market_value),
    equity: num(row.equity),
    pnlDay: num(row.pnl_day),
    pnlTotal: num(row.pnl_total),
  }
}

function toPosition(row: ShadowPositionSourceRow): ShadowPositionRow {
  const shares = Math.max(0, Math.trunc(num(row.shares)))
  const avgCost = num(row.avg_cost)
  const lastMark = num(row.last_mark, avgCost)
  const mark = lastMark > 0 ? lastMark : avgCost
  const pnl = openPositionNetPnl(shares, avgCost, mark)
  return {
    code: String(row.code || ''),
    name: String(row.name || ''),
    shares,
    avgCost,
    lastMark: mark,
    netPnlAmount: pnl.amount,
    netPnlPct: pnl.pct,
  }
}

function toEvent(row: ShadowEventSourceRow): ShadowEventRow {
  return {
    asOf: String(row.as_of || '').slice(0, 10),
    code: String(row.code || ''),
    name: String(row.name || ''),
    eventType: String(row.event_type || ''),
    price: optionalNum(row.price),
    qty: Math.trunc(num(row.qty)),
    reason: String(row.reason || ''),
    fees: feeMap(row.fees),
  }
}

function realizedClosePnls(events: ShadowEventSourceRow[]): number[] {
  const lots = new Map<string, { shares: number; avgCost: number }>()
  const closed: number[] = []
  for (const event of [...events].sort((a, b) => String(a.as_of || '').localeCompare(String(b.as_of || '')))) {
    if (!isFilledTrade(event)) continue
    const code = String(event.code || '')
    const qty = Math.trunc(num(event.qty))
    const price = optionalNum(event.price)
    if (!code || qty <= 0 || price == null || price <= 0) continue
    if (String(event.event_type) === 'buy') {
      applyBuyLot(lots, code, qty, price)
      continue
    }
    const pnl = applySellLot(lots, code, qty, price)
    if (pnl != null) closed.push(pnl)
  }
  return closed
}

function applyBuyLot(
  lots: Map<string, { shares: number; avgCost: number }>,
  code: string,
  qty: number,
  price: number,
) {
  const current = lots.get(code) || { shares: 0, avgCost: 0 }
  const shares = current.shares + qty
  lots.set(code, {
    shares,
    avgCost: shares > 0 ? (current.shares * current.avgCost + qty * price) / shares : 0,
  })
}

function applySellLot(
  lots: Map<string, { shares: number; avgCost: number }>,
  code: string,
  qty: number,
  price: number,
): number | null {
  const current = lots.get(code)
  if (!current || current.shares <= 0) return null
  const closedQty = Math.min(qty, current.shares)
  const remaining = current.shares - closedQty
  if (remaining <= 0) lots.delete(code)
  else lots.set(code, { shares: remaining, avgCost: current.avgCost })
  return (price - current.avgCost) * closedQty
}

function isFilledTrade(event: ShadowEventSourceRow): boolean {
  const type = String(event.event_type || '')
  if (type !== 'buy' && type !== 'sell') return false
  const payload = event.payload && typeof event.payload === 'object' ? event.payload as { status?: string } : {}
  return !payload.status || payload.status === 'filled'
}

function feeMap(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: Record<string, number> = {}
  for (const [key, value] of Object.entries(raw)) {
    const amount = num(value)
    if (Number.isFinite(amount)) out[key] = amount
  }
  return out
}

function collectKeys(value: unknown, keys = new Set<string>()): Set<string> {
  if (Array.isArray(value)) {
    for (const item of value) collectKeys(item, keys)
    return keys
  }
  if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      keys.add(key)
      collectKeys(child, keys)
    }
  }
  return keys
}

function rows<T>(result: { data: unknown[] | null; error: { message: string } | null }): T[] {
  if (result.error) throw new Error(result.error.message)
  return Array.isArray(result.data) ? result.data as T[] : []
}

function num(value: unknown, fallback = 0): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function optionalNum(value: unknown): number | null {
  if (value == null || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
