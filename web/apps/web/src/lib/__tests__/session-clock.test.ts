import { describe, expect, it } from 'vitest'
import {
  assessTradingDay,
  formatSessionClockContext,
  resolveSessionClock,
  toBeijingParts,
} from '@wyckoff/shared'

/** 2026-08-28 是周五。用 UTC 构造,避免测试机器时区影响结果。 */
function utc(iso: string): Date {
  return new Date(iso)
}

describe('toBeijingParts', () => {
  it('不看运行环境时区,固定按 UTC+8 换算', () => {
    // UTC 01:30 → 北京 09:30
    expect(toBeijingParts(utc('2026-08-28T01:30:00Z'))).toMatchObject({
      date: '2026-08-28', time: '09:30', weekday: 5,
    })
  })

  it('跨日:UTC 傍晚已经是北京第二天', () => {
    // UTC 周五 16:10 → 北京周六 00:10
    expect(toBeijingParts(utc('2026-08-28T16:10:00Z'))).toMatchObject({
      date: '2026-08-29', time: '00:10', weekday: 6,
    })
  })

  it('周日按 ISO 记为 7,不是 0', () => {
    expect(toBeijingParts(utc('2026-08-30T04:00:00Z')).weekday).toBe(7)
  })
})

describe('时段判定', () => {
  const at = (utcIso: string) => resolveSessionClock(utc(utcIso))

  it('09:25 到 09:30 之间是空档,不可下单', () => {
    // 集合竞价 09:25 结束,连续竞价 09:30 才开始
    expect(at('2026-08-28T01:27:00Z')).toMatchObject({ phase: 'pre-open', tradableByClock: false })
  })

  it('集合竞价与连续竞价都算可交易时段', () => {
    expect(at('2026-08-28T01:20:00Z')).toMatchObject({ phase: 'call-auction', tradableByClock: true })
    expect(at('2026-08-28T02:00:00Z')).toMatchObject({ phase: 'continuous', tradableByClock: true })
    expect(at('2026-08-28T06:58:00Z')).toMatchObject({ phase: 'call-auction', tradableByClock: true })
  })

  it('边界:11:30 收盘进午休,13:00 开盘回连续竞价', () => {
    expect(at('2026-08-28T03:30:00Z').phase).toBe('lunch-break')
    expect(at('2026-08-28T05:00:00Z').phase).toBe('continuous')
  })

  it('15:00 整已经收盘,不再可交易', () => {
    expect(at('2026-08-28T07:00:00Z')).toMatchObject({ phase: 'post-close', tradableByClock: false })
  })

  it('周末无论几点都是休市,且不存在节假日不确定性', () => {
    const saturday = at('2026-08-29T02:00:00Z')
    expect(saturday).toMatchObject({ phase: 'weekend', tradableByClock: false, holidayUnknown: false })
  })

  it('工作日一律带节假日未知标记 —— 调休和节假日算不出来', () => {
    expect(at('2026-08-28T02:00:00Z').holidayUnknown).toBe(true)
  })
})

describe('assessTradingDay', () => {
  const tradingHours = resolveSessionClock(utc('2026-08-28T02:00:00Z'))

  it('报价时间戳落在今天 → 证实今日开市', () => {
    expect(assessTradingDay(tradingHours, ['2026-08-28T02:00:00Z'])).toBe('confirmed-open')
  })

  it('时间戳停在前一天 → 很可能休市,不能报成开市', () => {
    expect(assessTradingDay(tradingHours, ['2026-08-27T07:00:00Z'])).toBe('likely-closed')
  })

  it('没有可用时间戳时说 unknown,不许猜一个方向', () => {
    expect(assessTradingDay(tradingHours, [])).toBe('unknown')
    expect(assessTradingDay(tradingHours, [null, '  '])).toBe('unknown')
  })

  it('收盘后拿不到判据:那时行情静止,分不出收盘与没开市', () => {
    const afterClose = resolveSessionClock(utc('2026-08-28T08:00:00Z'))
    expect(assessTradingDay(afterClose, ['2026-08-28T07:00:00Z'])).toBe('unknown')
  })

  it('周末直接判休市,不需要行情判据', () => {
    const weekend = resolveSessionClock(utc('2026-08-29T02:00:00Z'))
    expect(assessTradingDay(weekend, [])).toBe('likely-closed')
  })
})

describe('formatSessionClockContext', () => {
  it('每种情形都带上那句固定开头,时间取实测值', () => {
    const clock = resolveSessionClock(utc('2026-08-28T02:00:00Z'))
    const text = formatSessionClockContext(clock, 'confirmed-open')
    expect(text).toContain('当前北京时间:2026-08-28 10:00(UTC+8)')
    expect(text).toContain('周五')
  })

  it('非交易时段必须给出那句禁令,并限定到复盘与次日计划', () => {
    const clock = resolveSessionClock(utc('2026-08-28T08:00:00Z'))
    const text = formatSessionClockContext(clock, 'unknown')
    expect(text).toContain('当前不可盘中交易(原因:非交易时段')
    expect(text).toContain('T+1')
    expect(text).toContain('不要给「立刻买/立刻卖」的指令')
  })

  it('周末给的原因是非交易日,不是非交易时段', () => {
    const clock = resolveSessionClock(utc('2026-08-29T02:00:00Z'))
    expect(formatSessionClockContext(clock, 'likely-closed')).toContain('当前不可盘中交易(原因:非交易日)')
  })

  it('疑似节假日时按不可交易处理,而不是因为时钟对就放行', () => {
    const clock = resolveSessionClock(utc('2026-08-28T02:00:00Z'))
    const text = formatSessionClockContext(clock, 'likely-closed')
    expect(text).toContain('很可能是节假日休市')
    expect(text).toContain('按不可盘中交易处理')
  })

  it('没有判据时不假装确定,要求先确认今日开市', () => {
    const clock = resolveSessionClock(utc('2026-08-28T02:00:00Z'))
    const text = formatSessionClockContext(clock, 'unknown')
    expect(text).toContain('无法离线判定')
    expect(text).toContain('先确认今日确实开市')
    expect(text).not.toContain('今日确认在交易')
  })
})
