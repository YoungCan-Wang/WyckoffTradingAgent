/**
 * 计划任务与审批的纯逻辑：状态判定、cron 描述、时间显示。
 *
 * 从 app.js 搬过来的，行为保持一致 —— 这里只是把它从 DOM 构建里剥出来，
 * 好让三个页面共用同一套判定，也好测。
 */

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

export interface Schedule {
  id?: string
  name?: string
  cron?: string
  /** 任务内容（发给 agent 的那句话）。编辑表单要用。 */
  action?: string
  enabled?: boolean
  last_status?: string
  last_error?: string
  last_fired?: string
  next_run?: string
}

/** 界面上可推荐、点了才落盘的预置任务。 */
export interface Preset {
  id: string
  name: string
  cron: string
  action: string
}

export interface ApprovalItem {
  id: string
  tool_name?: string
  tool?: string
  summary?: string
  risk?: string
  source?: string
  schedule_id?: string
  created_at?: string
  args?: Record<string, unknown>
  risk_reason?: string
  nav_ratio?: number
}

export const failedStatus = (v?: string) => /fail|error|失败|异常/i.test(String(v || ''))
export const successStatus = (v?: string) => /success|complete|done|ok|成功|完成/i.test(String(v || ''))

export interface StateLabel {
  tone: '' | 'failed' | 'success'
  label: string
}

/** 未启用 ≠ 失败：三种状态要分开，混在一起会把「关掉的任务」报成故障。 */
export function scheduleState (s: Schedule): StateLabel {
  if (!s.enabled) return { tone: '', label: t('tasks.statusDisabled') }
  if (failedStatus(s.last_status) || s.last_error) return { tone: 'failed', label: t('tasks.statusFailed') }
  if (successStatus(s.last_status)) return { tone: 'success', label: t('tasks.statusSuccess') }
  return { tone: '', label: t('tasks.statusEnabled') }
}

/** 有故障的、已启用的计划 —— 「需要你处理」那一栏用它。 */
export const hasIssue = (s: Schedule) => Boolean(s.enabled && (failedStatus(s.last_status) || s.last_error))

/**
 * cron 转人话。认不出的原样显示 cron 串，不猜 —— 猜错比不翻译更糟。
 */
export function describeCron (cron?: string): string {
  const parts = String(cron || '').trim().split(/\s+/)
  if (parts.length !== 5) return t('schedules.rawCron', { cron: String(cron || '') })
  const [minute, hour, monthday, , weekday] = parts
  if (/^\*\/\d+$/.test(minute) && hour === '*') {
    return t('schedules.everyMinutes', { count: minute.slice(2) })
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
    if (weekday === '1-5') return t('schedules.everyWeekday', { time })
    // 每月几号要在「每天」之前判：monthday 是具体数字时 weekday 也是 '*'，
    // 顺序反了会把「每月 5 号」说成「每天」。
    if (/^\d+$/.test(monthday) && weekday === '*') {
      return t('schedules.everyMonthday', { day: String(Number(monthday)), time })
    }
    if (/^[0-6]$/.test(weekday) && monthday === '*') {
      return t('schedules.everyWeekOn', { weekday: weekdayName(Number(weekday)), time })
    }
    if (weekday === '*') return t('schedules.everyDay', { time })
  }
  return t('schedules.rawCron', { cron: String(cron || '') })
}

/** cron 的星期编号（0=周日）转名字。 */
export function weekdayName (value: number): string {
  return t(`schedules.weekday.${value}`)
}

/** 编辑器里的取值。cron 只是它的序列化形式，界面上不出现。 */
export type Repeat = 'weekday' | 'weekly' | 'monthly'

export interface Cadence {
  repeat: Repeat
  /** repeat === 'weekly' 时用；cron 编号，0=周日 */
  weekday: number
  /** repeat === 'monthly' 时用；1-31 */
  monthday: number
  hour: number
  minute: number
}

export const DEFAULT_CADENCE: Cadence = {
  repeat: 'weekday',
  weekday: 5,
  monthday: 1,
  // 09:25 —— A 股开盘前，和两个预置任务一致。默认值落在真实用途上，
  // 用户多半只改分钟不改小时。
  hour: 9,
  minute: 25
}

const clamp = (value: number, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, Math.trunc(Number(value) || 0)))

/** 编辑器取值 → cron 字符串。 */
export function buildCron (c: Cadence): string {
  const minute = clamp(c.minute, 0, 59)
  const hour = clamp(c.hour, 0, 23)
  if (c.repeat === 'monthly') {
    // 29-31 号遇上没那天的月份会跳过（标准 cron 语义），这是刻意的选择：
    // 「落到月末」需要改后端匹配逻辑，而跳过更可预测。
    return `${minute} ${hour} ${clamp(c.monthday, 1, 31)} * *`
  }
  if (c.repeat === 'weekly') {
    return `${minute} ${hour} * * ${clamp(c.weekday, 0, 6)}`
  }
  return `${minute} ${hour} * * 1-5`
}

/**
 * cron 字符串 → 编辑器取值。认不出返回 null。
 *
 * 编辑一个已有任务时用。认不出（比如手改过文件、或 TUI 写的 `*​/15` 形状）就返回
 * null，界面据此提示「这个时间表不支持在这里编辑」而不是**静默改成默认值** ——
 * 那样用户点开编辑再保存，触发时间就被悄悄换掉了。
 */
export function parseCron (cron?: string): Cadence | null {
  const parts = String(cron || '').trim().split(/\s+/)
  if (parts.length !== 5) return null
  const [minuteRaw, hourRaw, monthdayRaw, monthRaw, weekdayRaw] = parts
  if (monthRaw !== '*') return null
  if (!/^\d{1,2}$/.test(minuteRaw) || !/^\d{1,2}$/.test(hourRaw)) return null
  const minute = Number(minuteRaw)
  const hour = Number(hourRaw)
  if (minute > 59 || hour > 23) return null

  const base = { ...DEFAULT_CADENCE, hour, minute }
  if (monthdayRaw === '*' && weekdayRaw === '1-5') {
    return { ...base, repeat: 'weekday' }
  }
  if (monthdayRaw === '*' && /^[0-6]$/.test(weekdayRaw)) {
    return { ...base, repeat: 'weekly', weekday: Number(weekdayRaw) }
  }
  if (weekdayRaw === '*' && /^\d{1,2}$/.test(monthdayRaw)) {
    const day = Number(monthdayRaw)
    if (day < 1 || day > 31) return null
    return { ...base, repeat: 'monthly', monthday: day }
  }
  return null
}

/** 后端认可的风险理由。白名单之外的一律不显示 —— 别把内部代号漏给用户。 */
const KNOWN_REASONS = new Set([
  'destructive_action', 'over_nav', 'batch_over_nav', 'batch_malformed',
  'nav_unknown', 'write_tool', 'auto_narrow_tool',
  // 清除止损（cli/approval_policy.py 里升到 REVIEW 档时新增的）。
  // 白名单是「不把内部代号漏给用户」的防线，代价是后端加了理由、这里忘了加，
  // 那条理由就**静默消失** —— 而这一条恰恰是最需要说清楚的：审批卡上不写明
  // 「这会移除止损保护」，用户就不知道自己在批什么。
  'clears_stop_loss'
])

export function riskReasonText (item: ApprovalItem): string {
  const key = String(item.risk_reason || '')
  if (!key.startsWith('reason.')) return ''
  const name = key.slice('reason.'.length)
  if (!KNOWN_REASONS.has(name)) return ''
  const ratio = Number(item.nav_ratio) || 0
  // 占比只在与阈值相关的理由里才有意义，别给「清仓」硬贴一个百分比。
  if ((name === 'over_nav' || name === 'batch_over_nav') && ratio > 0) {
    return t(`approvals.reason.${name}`, { pct: (ratio * 100).toFixed(1) })
  }
  return t(`approvals.reason.${name}`)
}

/** 时间显示。认不出的原样回显，不要变成「Invalid Date」。 */
export function displayTime (value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(window.WyckoffI18n.getLang())
}
