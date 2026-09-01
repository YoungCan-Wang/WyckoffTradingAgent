/**
 * 威科夫图的结构化作图计划:模型只出判断,数字由代码算。
 *
 * 分界线落在哪一天、某根 K 线该叫 Spring 还是 LPS、为什么这么叫 —— 这些是读盘
 * 判断,只有模型能给。但吸筹区的价格带、预测线的每日取值是机械推导,交给模型
 * 等于让它编价格。所以这里的分工是硬的:模型给 phases / events / forecast 的
 * 锚点,代码从 K 线里算出 zones 和 forecast 序列。
 */

import { z } from 'zod'
import type { KlineRow } from './chat-tools'

export const WYCKOFF_PHASE_SCHEMA = z.object({
  /** Phase A-E。判断不出来就别硬凑,少标一个阶段好过标错。 */
  phase: z.enum(['A', 'B', 'C', 'D', 'E']),
  /** 大结构:吸筹 / 派发 / 上涨 / 下跌。zone 底色只画前两种。 */
  structure: z.enum(['accumulation', 'distribution', 'markup', 'markdown']),
  startDate: z.string(),
  endDate: z.string(),
  /** 这一段为什么这么定,中文一句话。 */
  reason: z.string(),
})

export const WYCKOFF_EVENT_SCHEMA = z.object({
  date: z.string(),
  /** 威科夫术语原文:SC / AR / ST / Spring / SOS / LPS / UTAD / BC…… */
  term: z.string(),
  /** 判为这个术语的理由,中文一句话。图上只显示术语,理由列在图下。 */
  reason: z.string(),
  side: z.enum(['above', 'below']),
})

export const WYCKOFF_FORECAST_SCHEMA = z.object({
  direction: z.enum(['up', 'down', 'sideways']),
  /** horizon 末端的目标价。前复权口径,与 K 线同一把尺子。 */
  targetPrice: z.number(),
  /** 交易日数。默认 30。 */
  horizonDays: z.number(),
  reason: z.string(),
})

export const WYCKOFF_CHART_PLAN_SCHEMA = z.object({
  phases: z.array(WYCKOFF_PHASE_SCHEMA),
  events: z.array(WYCKOFF_EVENT_SCHEMA),
  forecast: WYCKOFF_FORECAST_SCHEMA.nullable(),
})

export type WyckoffPhase = z.infer<typeof WYCKOFF_PHASE_SCHEMA>
export type WyckoffEvent = z.infer<typeof WYCKOFF_EVENT_SCHEMA>
export type WyckoffForecast = z.infer<typeof WYCKOFF_FORECAST_SCHEMA>
export type WyckoffChartPlan = z.infer<typeof WYCKOFF_CHART_PLAN_SCHEMA>

export interface WyckoffZone {
  structure: 'accumulation' | 'distribution'
  startDate: string
  endDate: string
  priceLow: number
  priceHigh: number
  /** 算价格带用到的收盘价根数,少于 5 根就别当回事。 */
  sampleSize: number
}

export interface WyckoffForecastPoint {
  date: string
  value: number
}

/** 密度带取 10%-90% 分位,不取极值 —— 极值一根异常 K 线就能把带子撑开。 */
const DENSITY_LOW_QUANTILE = 0.1
const DENSITY_HIGH_QUANTILE = 0.9
const MIN_ZONE_SAMPLE = 5
const DEFAULT_HORIZON_DAYS = 30
const MAX_HORIZON_DAYS = 120

function quantile(sorted: number[], q: number): number {
  if (sorted.length === 0) return 0
  const pos = q * (sorted.length - 1)
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (pos - lo)
}

/**
 * 丢掉日期落在 K 线之外的阶段与事件。
 *
 * 模型给的日期会编 —— 编一个不存在的交易日,画出来就是一条飘在空处的竖线。
 * 宁可少画一条,不画错一条。
 */
export function validateChartPlan(plan: WyckoffChartPlan, kline: KlineRow[]): WyckoffChartPlan {
  const dates = new Set(kline.map((row) => row.date))
  const first = kline[0]?.date ?? ''
  const last = kline.at(-1)?.date ?? ''
  const inCoverage = (date: string) => date >= first && date <= last

  return {
    // 阶段边界允许不精确落在交易日上(模型常给月初月末),只要求落在覆盖区间内且顺序正确。
    phases: plan.phases
      .filter((phase) => inCoverage(phase.startDate) && inCoverage(phase.endDate) && phase.startDate <= phase.endDate)
      .sort((a, b) => a.startDate.localeCompare(b.startDate)),
    // 事件必须钉在真实交易日上 —— 标注要挂在某一根 K 线上,没有这根就没处挂。
    events: plan.events
      .filter((event) => dates.has(event.date))
      .sort((a, b) => a.date.localeCompare(b.date)),
    forecast: plan.forecast && plan.forecast.targetPrice > 0 ? plan.forecast : null,
  }
}

/**
 * 把同一个结构的连续阶段并成一段,取其中 Phase B 的收盘价密度带做纵向范围。
 *
 * 纵向用**收盘价**而不是最高/最低价,这正好满足「排除 SC 的下影线与 AR 的上影线」——
 * 收盘价本身就不含影线,不需要再去识别哪根是 SC、哪根是 AR 然后特殊处理。
 * 横向铺满整个吸筹/派发结构(A 到 D),因为底色标的是这个区间,不只是 Phase B。
 */
export function deriveWyckoffZones(plan: WyckoffChartPlan, kline: KlineRow[]): WyckoffZone[] {
  const zones: WyckoffZone[] = []
  let run: WyckoffPhase[] = []

  const flush = () => {
    if (run.length === 0) return
    const structure = run[0]!.structure
    if (structure === 'accumulation' || structure === 'distribution') {
      const phaseB = run.find((phase) => phase.phase === 'B')
      const band = phaseB ? closeDensityBand(kline, phaseB.startDate, phaseB.endDate) : null
      if (band) {
        zones.push({
          structure,
          startDate: run[0]!.startDate,
          endDate: run.at(-1)!.endDate,
          priceLow: band.low,
          priceHigh: band.high,
          sampleSize: band.sampleSize,
        })
      }
    }
    run = []
  }

  for (const phase of plan.phases) {
    if (run.length > 0 && run[0]!.structure !== phase.structure) flush()
    run.push(phase)
  }
  flush()
  return zones
}

function closeDensityBand(kline: KlineRow[], startDate: string, endDate: string): { low: number; high: number; sampleSize: number } | null {
  const closes = kline
    .filter((row) => row.date >= startDate && row.date <= endDate)
    .map((row) => row.close)
    .sort((a, b) => a - b)
  // 样本太少算不出密度,直接不画 —— 一条假的价格带比没有底色更误导。
  if (closes.length < MIN_ZONE_SAMPLE) return null
  const low = quantile(closes, DENSITY_LOW_QUANTILE)
  const high = quantile(closes, DENSITY_HIGH_QUANTILE)
  return high > low ? { low, high, sampleSize: closes.length } : null
}

/**
 * 从最后一根 K 线线性推到目标价,生成预测序列。
 *
 * 未来的交易日期算不出来 —— 节假日不可推导(见 session-clock)。这里只跳周末,
 * 遇到节假日日期会偏几天。对一条 30 日的推演线,横轴偏几天无关紧要,但别把
 * 这些日期当成真实交易日引用。
 */
export function deriveForecastSeries(forecast: WyckoffForecast | null, kline: KlineRow[]): WyckoffForecastPoint[] {
  const last = kline.at(-1)
  if (!forecast || !last || forecast.targetPrice <= 0) return []
  const horizon = Math.min(
    Math.max(Math.round(forecast.horizonDays) || DEFAULT_HORIZON_DAYS, 1),
    MAX_HORIZON_DAYS,
  )
  const points: WyckoffForecastPoint[] = [{ date: last.date, value: last.close }]
  const step = (forecast.targetPrice - last.close) / horizon
  const cursor = new Date(`${last.date}T00:00:00Z`)
  if (Number.isNaN(cursor.getTime())) return []

  for (let i = 1; i <= horizon; i++) {
    do {
      cursor.setUTCDate(cursor.getUTCDate() + 1)
    } while (cursor.getUTCDay() === 0 || cursor.getUTCDay() === 6)
    points.push({ date: cursor.toISOString().slice(0, 10), value: last.close + step * i })
  }
  return points
}

/** 图下方的中文说明:术语配理由,一行一个。图上只挂术语,长句放这里。 */
export function formatChartPlanNotes(plan: WyckoffChartPlan): string[] {
  const notes = plan.phases.map((phase) => `Phase ${phase.phase}（${structureLabel(phase.structure)}） ${phase.startDate} 至 ${phase.endDate}：${phase.reason}`)
  notes.push(...plan.events.map((event) => `[${event.term}] ${event.date}：${event.reason}`))
  if (plan.forecast) {
    notes.push(`[推演] 未来 ${plan.forecast.horizonDays} 个交易日${directionLabel(plan.forecast.direction)}至 ${plan.forecast.targetPrice.toFixed(2)}：${plan.forecast.reason}`)
  }
  return notes
}

export function structureLabel(structure: WyckoffPhase['structure']): string {
  return structure === 'accumulation' ? '吸筹' : structure === 'distribution' ? '派发' : structure === 'markup' ? '上涨' : '下跌'
}

function directionLabel(direction: WyckoffForecast['direction']): string {
  return direction === 'up' ? '上行' : direction === 'down' ? '下行' : '横向震荡'
}
