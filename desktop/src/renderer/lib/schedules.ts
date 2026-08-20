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
  enabled?: boolean
  last_status?: string
  last_error?: string
  last_fired?: string
  next_run?: string
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
  const [minute, hour, , , weekday] = parts
  if (/^\*\/\d+$/.test(minute) && hour === '*') {
    return t('schedules.everyMinutes', { count: minute.slice(2) })
  }
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    const time = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`
    if (weekday === '1-5') return t('schedules.everyWeekday', { time })
    if (weekday === '*') return t('schedules.everyDay', { time })
  }
  return t('schedules.rawCron', { cron: String(cron || '') })
}

/** 后端认可的风险理由。白名单之外的一律不显示 —— 别把内部代号漏给用户。 */
const KNOWN_REASONS = new Set([
  'destructive_action', 'over_nav', 'batch_over_nav', 'batch_malformed',
  'nav_unknown', 'write_tool', 'auto_narrow_tool'
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
