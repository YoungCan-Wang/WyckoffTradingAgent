/**
 * 「定时任务」页：每个计划一张卡，带手动重跑。
 *
 * 重跑要跑完整一轮 agent（可能几分钟），所以按钮立刻禁用并就地显示进度 ——
 * 不要把用户丢去别的视图猜有没有在跑。
 */
import { useState } from 'react'
import { collect } from '../lib/ipc'
import { useIpc } from '../lib/useIpc'
import { scheduleState, describeCron, displayTime, type Schedule } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface SchedulesData {
  daemon_running?: boolean
  schedules?: Schedule[]
}

export function SchedulesPage () {
  const { data, loading, failed, reload } = useIpc<SchedulesData>('schedules')
  /**
   * 重跑结果放在页面级、按 id 存。
   *
   * 放在卡片里会被自己的刷新弄丢：重跑完要 reload 才能看到新状态，而 reload
   * 会重建卡片、连带清掉刚写好的那条结果 —— 用户点完只看到按钮恢复，不知道
   * 成功还是失败。
   */
  const [notes, setNotes] = useState<Record<string, Note>>({})

  if (loading) return <p className="empty">{t('schedules.loading')}</p>
  if (failed || !data) return <p className="empty">{t('schedules.readFailed')}</p>

  const list = data.schedules || []
  const daemonOn = Boolean(data.daemon_running)

  return (
    <>
      <div className={daemonOn ? 'status-banner on' : 'status-banner'}>
        {daemonOn ? t('schedules.daemonOn') : t('schedules.daemonOff')}
      </div>

      <div className="schedule-list">
        {list.length ? (
          list.map((s, i) => (
            <ScheduleCard
              key={s.id || i}
              schedule={s}
              note={notes[s.id || String(i)] || null}
              onNote={(n) => setNotes((prev) => ({ ...prev, [s.id || String(i)]: n }))}
              onRan={reload}
            />
          ))
        ) : (
          <p className="empty">{t('tasks.noneScheduled')}</p>
        )}
      </div>

      <button
        type="button"
        className="task-action"
        onClick={() => window.WyckoffApp?.navigate?.('tasks')}
      >
        {t('schedules.viewRuns')}
      </button>
    </>
  )
}

type Note = { text: string; failed: boolean } | null

interface CardProps {
  schedule: Schedule
  note: Note
  onNote: (n: Note) => void
  onRan: () => void
}

function ScheduleCard ({ schedule, note, onNote, onRan }: CardProps) {
  const [busy, setBusy] = useState(false)
  const state = scheduleState(schedule)

  const lastValue = !schedule.enabled
    ? state.label
    : schedule.last_fired
      ? `${displayTime(schedule.last_fired)} · ${state.label}`
      : t('schedules.neverRun')

  const label = schedule.last_fired ? t('schedules.rerun') : t('schedules.runOnce')

  const rerun = async () => {
    setBusy(true)
    onNote({ text: t('schedules.rerunStarted', { name: schedule.name || '' }), failed: false })
    try {
      const res = await collect('schedule_run', { id: schedule.id })
      const payload = (res || {}) as { ok?: boolean; error?: string; queued?: unknown[] }
      // ok=false 是「跑完了但失败」，与传输层异常不同，两者都要说清楚。
      if (payload.ok === false) {
        onNote({ text: t('schedules.rerunFailed', { error: payload.error || '' }), failed: true })
      } else {
        const queued = payload.queued || []
        onNote({
          text: queued.length
            ? t('schedules.rerunQueued', { count: queued.length })
            : t('schedules.rerunDone'),
          failed: false
        })
      }
    } catch (err) {
      onNote({ text: t('schedules.rerunFailed', { error: (err as Error)?.message || String(err) }), failed: true })
    }
    setBusy(false)
    // 重跑可能产生新审批，侧栏计数与本页状态都要跟上。
    window.WyckoffApp?.refreshApprovals?.()
    onRan()
  }

  return (
    <div className="schedule-card">
      <div>
        <div className="schedule-name">{schedule.name || '—'}</div>
        <div className="schedule-cadence">{describeCron(schedule.cron)} · {schedule.cron || '—'}</div>
      </div>
      <div>
        <div className="schedule-label">{t('schedules.lastRun')}</div>
        <div className={`schedule-value ${state.tone}`}>{schedule.last_error || lastValue}</div>
      </div>
      <div>
        <div className="schedule-label">{t('schedules.nextRun')}</div>
        <div className="schedule-value tnum">
          {schedule.next_run ? displayTime(schedule.next_run) : '—'}
        </div>
      </div>
      {/* 没有 id 的任务点了必然报错，直接禁用 */}
      <button type="button" className="schedule-rerun" disabled={busy || !schedule.id} onClick={rerun}>
        {busy ? t('schedules.rerunning') : label}
      </button>
      {note ? (
        <p className={note.failed ? 'schedule-rerun-note failed' : 'schedule-rerun-note'}>
          {note.text}
        </p>
      ) : null}
    </div>
  )
}
