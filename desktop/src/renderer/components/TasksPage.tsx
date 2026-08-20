/**
 * 「任务运行」页：把审批与计划任务汇总到一处。
 *
 * 只展示后端真实返回的状态 —— 不推测不存在的运行记录。读不到就说读不到，
 * 不要把「拿不到状态」画成「未运行」，那是两回事。
 */
import { useIpc } from '../lib/useIpc'
import {
  scheduleState, hasIssue, describeCron, displayTime,
  type Schedule, type ApprovalItem
} from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface ApprovalsData { count?: number; items?: ApprovalItem[] }
interface SchedulesData { daemon_running?: boolean; schedules?: Schedule[] }

export function TasksPage () {
  const approvals = useIpc<ApprovalsData>('approve_list')
  const schedules = useIpc<SchedulesData>('schedules')

  if (approvals.loading || schedules.loading) return <p className="empty">{t('tab.loading')}</p>

  const pending = (approvals.data && approvals.data.items) || []
  const all = (schedules.data && schedules.data.schedules) || []
  const enabled = all.filter((s) => s.enabled)
  const issues = all.filter(hasIssue)
  const daemonOn = Boolean(schedules.data && schedules.data.daemon_running)
  // 读失败与「真的没有」分开说。
  const readFailed = approvals.failed || schedules.failed

  return (
    <>
      {readFailed ? <p className="empty">{t('schedules.statusReadFailed')}</p> : null}

      <div className={daemonOn ? 'status-banner on' : 'status-banner'}>
        {daemonOn ? t('schedules.daemonOn') : t('schedules.daemonOff')}
      </div>

      {enabled.length || pending.length || issues.length ? (
        <div className="task-metrics">
          {enabled.length ? <Metric value={enabled.length} label={t('tasks.enabled')} /> : null}
          {pending.length ? <Metric value={pending.length} label={t('tasks.pending')} /> : null}
          {issues.length ? <Metric value={issues.length} label={t('tasks.issues')} /> : null}
        </div>
      ) : null}

      <Section title={t('tasks.attention')} count={pending.length + issues.length}>
        {!pending.length && !issues.length ? (
          <p className="empty">{t('tasks.noneAttention')}</p>
        ) : null}
        {pending.map((item) => (
          <TaskRow
            key={`a-${item.id}`}
            tone="pending"
            title={item.summary || item.tool_name || t('approvals.defaultItem')}
            meta={[t('tasks.sourceApproval'), item.tool_name, item.source].filter(Boolean).join(' · ')}
            state={t('tasks.statusPending')}
            time={displayTime(item.created_at)}
            action={t('tasks.openApprovals')}
            onAction={() => window.WyckoffApp?.navigate?.('approvals')}
          />
        ))}
        {issues.map((s, i) => <ScheduleRow key={`i-${s.id || i}`} schedule={s} issue />)}
      </Section>

      <Section title={t('tasks.scheduled')} count={all.length}>
        {!all.length ? <p className="empty">{t('tasks.noneScheduled')}</p> : null}
        {all.map((s, i) => <ScheduleRow key={`s-${s.id || i}`} schedule={s} issue={false} />)}
      </Section>
    </>
  )
}

function ScheduleRow ({ schedule, issue }: { schedule: Schedule; issue: boolean }) {
  const state = scheduleState(schedule)
  const last = schedule.last_fired
    ? t('tasks.lastRun', { time: displayTime(schedule.last_fired) })
    : t('tasks.neverRun')
  return (
    <TaskRow
      tone={issue ? 'failed' : state.tone}
      title={schedule.name || '—'}
      meta={`${t('tasks.sourceSchedule')} · ${describeCron(schedule.cron)} · ${last}`}
      state={state.label}
      time={schedule.next_run ? t('tasks.nextRun', { time: displayTime(schedule.next_run) }) : '—'}
      action={t('tasks.openSchedules')}
      onAction={() => window.WyckoffApp?.navigate?.('schedules')}
    />
  )
}

interface RowProps {
  tone?: string
  title: string
  meta: string
  state: string
  time?: string
  action?: string
  onAction?: () => void
}

function TaskRow ({ tone, title, meta, state, time, action, onAction }: RowProps) {
  return (
    <div className="task-row">
      <span className={`task-dot ${tone || ''}`} />
      <div>
        <div className="task-title">{title}</div>
        <div className="task-meta">{meta}</div>
      </div>
      <div className="task-side">
        <span className="task-state">{state}</span>
        <span className="task-time">{time || '—'}</span>
        {action ? (
          <button type="button" className="task-action" onClick={onAction}>{action}</button>
        ) : null}
      </div>
    </div>
  )
}

function Metric ({ value, label }: { value: number; label: string }) {
  return (
    <div className="task-metric">
      <b className="tnum">{String(value)}</b>
      <span>{label}</span>
    </div>
  )
}

function Section (
  { title, count, children }: { title: string; count: number; children: React.ReactNode }
) {
  return (
    <section className="task-section">
      <div className="task-section-h">
        <h2>{title}</h2>
        <span className="tnum">{String(count)}</span>
      </div>
      <div className="task-list">{children}</div>
    </section>
  )
}
