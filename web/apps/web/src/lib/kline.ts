import { TICKFLOW_PURCHASE, buildKlineDataQuality, isCnSymbol, normalizeTickFlowSymbol, normalizeTushareCode } from '@wyckoff/shared'
import type { KlineDataQuality, KlineRow } from '@wyckoff/shared'

import { formatTimestampDate } from './format'
import { supabase } from './supabase'

type Fetcher = typeof globalThis.fetch

function parseRowArray(rows: unknown[]): KlineRow[] {
  return (rows as Record<string, unknown>[])
    .map((r) => ({
      date: String(r.date || r.trade_date || '').replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
      open: Number(r.open || 0),
      high: Number(r.high || 0),
      low: Number(r.low || 0),
      close: Number(r.close || 0),
      volume: Number(r.volume || r.vol || 0),
    }))
    .filter((d) => d.date && d.close > 0)
}

function parseTickFlowTable(table: Record<string, unknown[]>): KlineRow[] {
  const timestamps = Array.isArray(table.timestamp) ? table.timestamp : []
  if (timestamps.length === 0) return []
  const open = table.open || [], high = table.high || [], low = table.low || []
  const close = table.close || [], volume = table.volume || []
  return timestamps
    .map((ts, i) => ({
      date: formatTimestampDate(ts),
      open: Number(open[i] || 0),
      high: Number(high[i] || 0),
      low: Number(low[i] || 0),
      close: Number(close[i] || 0),
      volume: Number(volume[i] || 0),
    }))
    .filter((d) => d.date && d.close > 0)
}

function findTickFlowTable(data: unknown, symbol: string): Record<string, unknown[]> | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null
  const obj = data as Record<string, unknown>
  if (Array.isArray(obj.timestamp)) return obj as Record<string, unknown[]>
  const direct = obj[symbol]
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    const table = direct as Record<string, unknown>
    if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
  }
  for (const value of Object.values(obj)) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const table = value as Record<string, unknown>
      if (Array.isArray(table.timestamp)) return table as Record<string, unknown[]>
    }
  }
  return null
}

function parseKlinePayload(payload: unknown, symbol: string): KlineRow[] {
  if (!payload || typeof payload !== 'object') return []
  const root = payload as Record<string, unknown>
  const data = root.data
  if (Array.isArray(data)) return parseRowArray(data)
  if (Array.isArray(root.records)) return parseRowArray(root.records)
  const table = findTickFlowTable(data, symbol)
  return table ? parseTickFlowTable(table) : []
}

async function readTickFlowError(resp: Response): Promise<string> {
  const text = await resp.text().catch(() => '')
  try {
    const json = JSON.parse(text)
    return String(json?.error?.message || json?.message || json?.error || '').trim()
  } catch {
    return text.slice(0, 160).trim()
  }
}

function tickFlowUpgradeError(status: number, detail: string): Error {
  const reason = detail ? `：${detail}` : ''
  return new Error(`TickFlow 数据源返回 ${status}${reason}。可能是数据权限、额度或并发限制，请升级数据源后重试：${TICKFLOW_PURCHASE}`)
}

async function tusharePostWithFetch(fetcher: Fetcher, token: string, api_name: string, params: Record<string, string>, fields: string) {
  const resp = await fetcher('/api/llm-proxy/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Target-URL': 'https://api.tushare.pro' },
    body: JSON.stringify({ api_name, token, params, fields }),
  })
  if (!resp.ok) return null
  return (await resp.json()) as { data?: { fields?: string[]; items?: unknown[][] } }
}

async function tusharePost(token: string, api_name: string, params: Record<string, string>, fields: string) {
  return tusharePostWithFetch(globalThis.fetch, token, api_name, params, fields)
}

export async function fetchKlineViaTushare(code: string, token: string, startDate: string, endDate: string): Promise<KlineRow[]> {
  const tsCode = normalizeTushareCode(code)
  const [dailyJson, adjJson] = await Promise.all([
    tusharePost(token, 'daily', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,open,high,low,close,vol'),
    tusharePost(token, 'adj_factor', { ts_code: tsCode, start_date: startDate, end_date: endDate }, 'trade_date,adj_factor'),
  ])
  const items = dailyJson?.data?.items
  if (!Array.isArray(items) || items.length === 0) return []

  const adjItems = adjJson?.data?.items
  if (!Array.isArray(adjItems) || adjItems.length === 0) return []
  const adjMap = new Map<string, number>()
  let latestDate = ''
  for (const row of adjItems) {
    const dt = String(row[0])
    adjMap.set(dt, Number(row[1]))
    if (dt > latestDate) latestDate = dt
  }
  const latestFactor = adjMap.get(latestDate) || 1

  return items.map(row => {
    const dt = String(row[0] || '')
    const factor = adjMap.get(dt) || latestFactor
    const ratio = factor / latestFactor
    return {
      date: dt.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'),
      open: Number(row[1] || 0) * ratio, high: Number(row[2] || 0) * ratio,
      low: Number(row[3] || 0) * ratio, close: Number(row[4] || 0) * ratio,
      volume: Number(row[5] || 0),
    }
  }).filter(d => d.date && d.close > 0)
}

export async function fetchKlineViaTickFlow(code: string, apiKey: string): Promise<KlineRow[]> {
  const symbol = normalizeTickFlowSymbol(code)
  const params = new URLSearchParams({
    symbol, period: '1d', count: '320', adjust: 'forward',
  })
  let apiError: Error | null = null
  const resp = await fetch(`/api/llm-proxy/v1/klines?${params}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (resp.ok) {
    const rows = parseKlinePayload(await resp.json(), symbol)
    if (rows.length) return rows.sort((a, b) => a.date.localeCompare(b.date)).slice(-320)
  } else {
    apiError = tickFlowUpgradeError(resp.status, await readTickFlowError(resp))
  }
  const batchParams = new URLSearchParams({ symbols: symbol, period: '1d', count: '320', adjust: 'forward' })
  const batchResp = await fetch(`/api/llm-proxy/v1/klines/batch?${batchParams}`, {
    headers: { 'x-api-key': apiKey, 'X-Target-URL': 'https://api.tickflow.org' },
  })
  if (!batchResp.ok) throw tickFlowUpgradeError(batchResp.status, await readTickFlowError(batchResp))
  const batchRows = parseKlinePayload(await batchResp.json(), symbol).sort((a, b) => a.date.localeCompare(b.date)).slice(-320)
  if (batchRows.length) return batchRows
  if (apiError) throw apiError
  return []
}


export async function getUserDataKeys(userId: string): Promise<{ tickflow: string | null; tushare: string | null }> {
  const { data } = await supabase
    .from('user_settings')
    .select('tickflow_api_key, tushare_token')
    .eq('user_id', userId)
    .single()
  return {
    tickflow: String(data?.tickflow_api_key || '').trim() || null,
    tushare: String(data?.tushare_token || '').trim() || null,
  }
}

export async function fetchKlineWithQuality(
  code: string,
  keys: { tickflow: string | null; tushare: string | null },
  _userId: string,
): Promise<{ data: KlineRow[]; quality: KlineDataQuality }> {
  const end = new Date(); end.setDate(end.getDate() - 1)
  const start = new Date(); start.setDate(start.getDate() - 500)
  const fmtCompact = (d: Date) => d.toISOString().slice(0, 10).replace(/-/g, '')
  const isCn = isCnSymbol(code)
  let tickflowError: Error | null = null

  if (keys.tickflow) {
    try {
      const r = await fetchKlineViaTickFlow(code, keys.tickflow)
      if (r.length) return { data: r, quality: buildKlineDataQuality('tickflow', 320, r) }
    } catch (err) { tickflowError = err instanceof Error ? err : new Error(String(err)) }
  }
  if (isCn && keys.tushare) {
    if (tickflowError) console.warn(`[kline] TickFlow failed for ${code}, falling back to Tushare:`, tickflowError.message)
    try {
      const r = await fetchKlineViaTushare(code, keys.tushare, fmtCompact(start), fmtCompact(end))
      if (r.length) {
        const data = r.sort((a, b) => a.date.localeCompare(b.date)).slice(-320)
        return { data, quality: buildKlineDataQuality('tushare', 320, data, Boolean(keys.tickflow)) }
      }
    } catch { /* fallthrough */ }
  }
  if (tickflowError) throw tickflowError
  const suffixHint = isCn ? '' : '美股/港股请使用 TickFlow 标准代码（如 AAPL.US / 00700.HK）。'
  throw new Error(`无法获取K线数据。${suffixHint}请检查股票代码、TickFlow Key 或稍后重试：${TICKFLOW_PURCHASE}`)
}

export async function fetchKline(
  code: string,
  keys: { tickflow: string | null; tushare: string | null },
  userId: string,
): Promise<KlineRow[]> {
  return (await fetchKlineWithQuality(code, keys, userId)).data
}
