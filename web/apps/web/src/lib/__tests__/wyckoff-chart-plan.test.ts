import { describe, expect, it } from 'vitest'
import {
  deriveForecastSeries,
  deriveWyckoffZones,
  formatChartPlanNotes,
  validateChartPlan,
  type KlineRow,
  type WyckoffChartPlan,
} from '@wyckoff/shared'

/** 2024-01-01 起连续自然日,收盘价由 closes 指定。 */
function buildKline(closes: number[], startDay = 1): KlineRow[] {
  return closes.map((close, index) => {
    const day = String(startDay + index).padStart(2, '0')
    return {
      date: `2024-01-${day}`,
      open: close,
      high: close + 1,
      low: close - 1,
      close,
      volume: 1000,
    }
  })
}

const EMPTY_PLAN: WyckoffChartPlan = { phases: [], events: [], forecast: null }

describe('validateChartPlan', () => {
  const kline = buildKline([10, 11, 12, 13, 14])

  it('丢掉不存在的交易日上的事件 —— 标注没有 K 线可挂', () => {
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      events: [
        { date: '2024-01-03', term: 'Spring', reason: '缩量下破后收回', side: 'below' },
        { date: '2024-01-04', term: 'SOS', reason: '放量突破', side: 'below' },
      ],
    }
    // 01-04 存在于 kline,01-03 也存在;换一个不存在的日期验证过滤
    const withBogus: WyckoffChartPlan = {
      ...plan,
      events: [...plan.events, { date: '2024-06-15', term: 'UTAD', reason: '编的日期', side: 'above' }],
    }
    const result = validateChartPlan(withBogus, kline)
    expect(result.events.map((event) => event.date)).toEqual(['2024-01-03', '2024-01-04'])
  })

  it('阶段超出数据覆盖区间就丢掉', () => {
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [
        { phase: 'B', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-04', reason: '区间震荡' },
        { phase: 'C', structure: 'accumulation', startDate: '2023-06-01', endDate: '2023-07-01', reason: '早于覆盖区间' },
      ],
    }
    const result = validateChartPlan(plan, kline)
    expect(result.phases).toHaveLength(1)
    expect(result.phases[0]!.phase).toBe('B')
  })

  it('起止颠倒的阶段丢掉', () => {
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [{ phase: 'A', structure: 'accumulation', startDate: '2024-01-04', endDate: '2024-01-02', reason: '倒序' }],
    }
    expect(validateChartPlan(plan, kline).phases).toHaveLength(0)
  })

  it('阶段按开始日期排序', () => {
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [
        { phase: 'C', structure: 'accumulation', startDate: '2024-01-04', endDate: '2024-01-05', reason: 'c' },
        { phase: 'A', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-02', reason: 'a' },
      ],
    }
    expect(validateChartPlan(plan, kline).phases.map((p) => p.phase)).toEqual(['A', 'C'])
  })

  it('目标价非正的推演直接丢掉', () => {
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      forecast: { direction: 'up', targetPrice: 0, horizonDays: 30, reason: '无效' },
    }
    expect(validateChartPlan(plan, kline).forecast).toBeNull()
  })
})

describe('deriveWyckoffZones', () => {
  it('纵向范围取 Phase B 的收盘价密度带,不含影线', () => {
    // 收盘价 10..19,最高价各 +1、最低价各 -1。带子必须落在收盘价范围内。
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [
        { phase: 'A', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-02', reason: 'a' },
        { phase: 'B', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-10', reason: 'b' },
      ],
    }
    const zones = deriveWyckoffZones(plan, kline)
    expect(zones).toHaveLength(1)
    // 10%-90% 分位落在 10.9 与 18.1,不会碰到 low=9 或 high=20 的影线端。
    expect(zones[0]!.priceLow).toBeGreaterThan(10)
    expect(zones[0]!.priceHigh).toBeLessThan(19)
    expect(zones[0]!.sampleSize).toBe(10)
  })

  it('横向铺满整个吸筹结构,不只是 Phase B', () => {
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [
        { phase: 'A', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-02', reason: 'a' },
        { phase: 'B', structure: 'accumulation', startDate: '2024-01-03', endDate: '2024-01-08', reason: 'b' },
        { phase: 'D', structure: 'accumulation', startDate: '2024-01-09', endDate: '2024-01-10', reason: 'd' },
      ],
    }
    const zones = deriveWyckoffZones(plan, kline)
    expect(zones[0]!.startDate).toBe('2024-01-01')
    expect(zones[0]!.endDate).toBe('2024-01-10')
  })

  it('没有 Phase B 就不画底色 —— 密度带没有依据', () => {
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [{ phase: 'A', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-10', reason: 'a' }],
    }
    expect(deriveWyckoffZones(plan, kline)).toHaveLength(0)
  })

  it('Phase B 样本不足 5 根就不画', () => {
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [{ phase: 'B', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-03', reason: 'b' }],
    }
    expect(deriveWyckoffZones(plan, kline)).toHaveLength(0)
  })

  it('上涨/下跌阶段不画底色 —— 底色只标吸筹和派发', () => {
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [{ phase: 'B', structure: 'markup', startDate: '2024-01-01', endDate: '2024-01-10', reason: 'e' }],
    }
    expect(deriveWyckoffZones(plan, kline)).toHaveLength(0)
  })

  it('吸筹后接派发拆成两块底色', () => {
    const kline = buildKline([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29], 1)
    const plan: WyckoffChartPlan = {
      ...EMPTY_PLAN,
      phases: [
        { phase: 'B', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-08', reason: 'acc' },
        { phase: 'B', structure: 'distribution', startDate: '2024-01-12', endDate: '2024-01-20', reason: 'dist' },
      ],
    }
    const zones = deriveWyckoffZones(plan, kline)
    expect(zones.map((zone) => zone.structure)).toEqual(['accumulation', 'distribution'])
  })
})

describe('deriveForecastSeries', () => {
  const kline = buildKline([10, 11, 12, 13, 14])

  it('从最后一根 K 线线性推到目标价', () => {
    const points = deriveForecastSeries(
      { direction: 'up', targetPrice: 20, horizonDays: 10, reason: 'r' },
      kline,
    )
    expect(points).toHaveLength(11)
    expect(points[0]).toEqual({ date: '2024-01-05', value: 14 })
    expect(points.at(-1)!.value).toBeCloseTo(20, 6)
  })

  it('跳过周末 —— 但这些日期不是真实交易日,节假日算不出来', () => {
    // 2024-01-05 是周五,下一个点应落在 01-08(周一)。
    const points = deriveForecastSeries(
      { direction: 'up', targetPrice: 20, horizonDays: 2, reason: 'r' },
      kline,
    )
    expect(points[1]!.date).toBe('2024-01-08')
    expect(points[2]!.date).toBe('2024-01-09')
  })

  it('forecast 为 null 时返回空数组', () => {
    expect(deriveForecastSeries(null, kline)).toEqual([])
  })

  it('K 线为空时返回空数组', () => {
    expect(deriveForecastSeries({ direction: 'up', targetPrice: 20, horizonDays: 30, reason: 'r' }, [])).toEqual([])
  })

  it('horizonDays 为 0 时退回默认 30 日,不产生除零', () => {
    const points = deriveForecastSeries(
      { direction: 'up', targetPrice: 20, horizonDays: 0, reason: 'r' },
      kline,
    )
    expect(points).toHaveLength(31)
    expect(points.every((point) => Number.isFinite(point.value))).toBe(true)
  })

  it('horizonDays 过大时截到上限', () => {
    const points = deriveForecastSeries(
      { direction: 'up', targetPrice: 20, horizonDays: 9999, reason: 'r' },
      kline,
    )
    expect(points).toHaveLength(121)
  })
})

describe('formatChartPlanNotes', () => {
  it('术语与理由成对列出,阶段用中文结构名', () => {
    const notes = formatChartPlanNotes({
      phases: [{ phase: 'C', structure: 'accumulation', startDate: '2024-01-01', endDate: '2024-01-10', reason: '缩量测试支撑' }],
      events: [{ date: '2024-01-05', term: 'Spring', reason: '下破后当日收回' }],
      forecast: { direction: 'up', targetPrice: 20.5, horizonDays: 30, reason: '结构完成待突破' },
    } as WyckoffChartPlan)
    expect(notes[0]).toContain('Phase C（吸筹）')
    expect(notes[0]).toContain('缩量测试支撑')
    expect(notes[1]).toBe('[Spring] 2024-01-05：下破后当日收回')
    expect(notes[2]).toContain('未来 30 个交易日上行至 20.50')
  })

  it('没有推演时不产生推演行', () => {
    expect(formatChartPlanNotes(EMPTY_PLAN)).toEqual([])
  })
})
