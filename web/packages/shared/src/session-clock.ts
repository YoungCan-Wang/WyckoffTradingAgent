/**
 * 北京时间与 A 股交易时段:算得出的部分算,算不出的部分说清楚。
 *
 * 时段规则(周末、连续竞价、集合竞价)是纯计算,离线可判。节假日不是 ——
 * 它由国务院逐年公布,没有算法能推出来,调休更是没有规律。所以这里不猜:
 * 工作日只报到 `weekday-open`(按时段规则可交易),节假日的可能性作为已知
 * 不确定性一并交出去,由调用方用行情时间戳这类证据去证实。
 */

export type SessionPhase =
  | 'pre-open'      // 开盘前(09:15 之前)
  | 'call-auction'  // 集合竞价 09:15-09:25 / 14:57-15:00
  | 'continuous'    // 连续竞价 09:30-11:30 / 13:00-15:00
  | 'lunch-break'   // 午间休市 11:30-13:00
  | 'post-close'    // 收盘后
  | 'weekend'       // 周末

export interface SessionClock {
  /** 「YYYY-MM-DD HH:MM」北京时间,用于回复开头那一行。 */
  beijingText: string
  beijingDate: string
  beijingTime: string
  /** 1=周一 … 7=周日,按北京时间算。 */
  weekday: number
  phase: SessionPhase
  /** 时段规则允许下单。节假日未计入 —— 见 holidayUnknown。 */
  tradableByClock: boolean
  /** 落在工作日时为 true:今天可能是节假日或调休,离线无法判定。 */
  holidayUnknown: boolean
}

const MINUTE = 60
const CALL_AUCTION_MORNING = [9 * 60 + 15, 9 * 60 + 25] as const
const CONTINUOUS_MORNING = [9 * 60 + 30, 11 * 60 + 30] as const
const CONTINUOUS_AFTERNOON = [13 * 60, 14 * 60 + 57] as const
const CALL_AUCTION_CLOSING = [14 * 60 + 57, 15 * 60] as const

/**
 * 把任意时刻换成北京时间(UTC+8)。不依赖运行环境的本地时区 ——
 * Worker 跑在 UTC,桌面端跑在用户本地,两边必须得出同一个答案。
 */
export function toBeijingParts(now: Date): { date: string; time: string; weekday: number; minutes: number } {
  const shifted = new Date(now.getTime() + 8 * 60 * MINUTE * 1000)
  const date = shifted.toISOString().slice(0, 10)
  const hour = shifted.getUTCHours()
  const minute = shifted.getUTCMinutes()
  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
  // getUTCDay(): 0=周日。转成 ISO 的 1=周一 … 7=周日。
  const weekday = shifted.getUTCDay() === 0 ? 7 : shifted.getUTCDay()
  return { date, time, weekday, minutes: hour * 60 + minute }
}

function inRange(minutes: number, range: readonly [number, number]): boolean {
  return minutes >= range[0] && minutes < range[1]
}

function resolvePhase(weekday: number, minutes: number): SessionPhase {
  if (weekday >= 6) return 'weekend'
  if (inRange(minutes, CALL_AUCTION_MORNING)) return 'call-auction'
  if (inRange(minutes, CONTINUOUS_MORNING)) return 'continuous'
  if (inRange(minutes, CONTINUOUS_AFTERNOON)) return 'continuous'
  if (inRange(minutes, CALL_AUCTION_CLOSING)) return 'call-auction'
  // 09:25-09:30 是竞价结束到开盘之间的空档,归到开盘前 —— 不能落进 post-close。
  if (minutes < CONTINUOUS_MORNING[0]) return 'pre-open'
  if (minutes < CONTINUOUS_AFTERNOON[0]) return 'lunch-break'
  return 'post-close'
}

export function resolveSessionClock(now: Date): SessionClock {
  const { date, time, weekday, minutes } = toBeijingParts(now)
  const phase = resolvePhase(weekday, minutes)
  const tradable = phase === 'continuous' || phase === 'call-auction'
  return {
    beijingText: `${date} ${time}`,
    beijingDate: date,
    beijingTime: time,
    weekday,
    phase,
    tradableByClock: tradable,
    holidayUnknown: weekday <= 5,
  }
}

const PHASE_LABEL: Record<SessionPhase, string> = {
  'pre-open': '开盘前',
  'call-auction': '集合竞价',
  continuous: '连续竞价',
  'lunch-break': '午间休市',
  'post-close': '收盘后',
  weekend: '周末休市',
}

export function sessionPhaseLabel(phase: SessionPhase): string {
  return PHASE_LABEL[phase]
}

export type TradingDayEvidence = 'confirmed-open' | 'likely-closed' | 'unknown'

/**
 * 用行情时间戳证实「今天到底开没开」。
 *
 * 节假日算不出来,但能观测:交易时段里报价时间戳落在今天 → 今天在交易。
 * 时间戳停在往前某一天 → 大概率休市(也可能是数据源滞后,所以只说 likely)。
 * 收盘后拿不到判据 —— 那时行情静止,分不出「今天收盘了」和「今天没开」。
 */
export function assessTradingDay(clock: SessionClock, quoteTimestamps: Array<string | null>): TradingDayEvidence {
  if (clock.phase === 'weekend') return 'likely-closed'
  if (!clock.tradableByClock) return 'unknown'
  const dates = quoteTimestamps
    .filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    .map((value) => toBeijingParts(new Date(value)))
    .filter((parts) => !Number.isNaN(Date.parse(parts.date)))
  if (dates.length === 0) return 'unknown'
  if (dates.some((parts) => parts.date === clock.beijingDate)) return 'confirmed-open'
  return 'likely-closed'
}

/**
 * 当轮注入的时间与时段口径。走 user 消息,不进 system —— system 要保持字节
 * 稳定给 prompt cache 用,时间每轮都变,写进去等于每轮击穿缓存。
 */
export function formatSessionClockContext(clock: SessionClock, evidence: TradingDayEvidence): string {
  const lines = [
    '## 当前时间与交易时段(本轮实测,不是推算)',
    `当前北京时间:${clock.beijingText}(UTC+8),周${'一二三四五六日'[clock.weekday - 1]}。`,
    `时段:${sessionPhaseLabel(clock.phase)}。`,
    '回复开头必须先写这一行:「当前北京时间:' + clock.beijingText + '(UTC+8)」。不要自己推断或编造时间。',
  ]

  if (clock.phase === 'weekend') {
    lines.push('周末休市,当前不可盘中交易(原因:非交易日)。只做盘后复盘、次日计划与 T+1 委托策略,不要给「立刻买/立刻卖」的指令。')
    return lines.join('\n')
  }

  if (!clock.tradableByClock) {
    lines.push(`当前不可盘中交易(原因:非交易时段,${sessionPhaseLabel(clock.phase)})。只做盘后复盘、次日计划与 T+1 委托策略,不要给「立刻买/立刻卖」的指令。`)
    lines.push('连续竞价 09:30-11:30 / 13:00-15:00,集合竞价 09:15-09:25 与 14:57-15:00。')
    return lines.join('\n')
  }

  if (evidence === 'confirmed-open') {
    lines.push('本轮行情时间戳落在今天,今日确认在交易,处于可盘中交易时段。')
    return lines.join('\n')
  }

  if (evidence === 'likely-closed') {
    lines.push('注意:时段规则上属于交易时间,但本轮行情时间戳不是今天 —— 今天很可能是节假日休市(也可能是数据源滞后)。按不可盘中交易处理,除非另有证据表明今日开市。')
    return lines.join('\n')
  }

  lines.push('时段规则上属于交易时间。节假日与调休无法离线判定,本轮也没有取到可用行情时间戳作为判据;如果要给盘中交易指令,先确认今日确实开市。')
  return lines.join('\n')
}
