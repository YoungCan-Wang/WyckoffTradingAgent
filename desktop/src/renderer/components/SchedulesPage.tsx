/**
 * 「定时任务」页：已有任务在上，推荐在下。
 *
 * ## 布局
 *
 * 一个任务都没有时页面不能是死的 —— 上方显示「还没有定时任务」，下方给几个可以
 * 直接点「添加」的推荐。已经添加过的从推荐里剔掉（后端按 id 判断）。
 *
 * 两个区共用同一套 grid 列宽（`.sched-grid`），所以左右边缘天然对齐，不用手调。
 * 推荐项用更轻的样式：没有边框，只有一行，视觉重量明显低于真任务。
 *
 * ## 编辑是模式开关
 *
 * 不是每张卡上挂两个按钮 —— 那样按钮会把真正的内容挤到中间（持仓页同样的取舍）。
 * 展开的表单替换掉那张卡本身。
 *
 * ## 状态放页面级
 *
 * 编辑态、忙碌态、错误都按 id 存在页面上。放在卡片里会被 reload() 弄丢：写完必须
 * 重拉，而重拉会重建卡片、连带清掉刚写好的状态。
 */
import { useCallback, useState } from 'react'
import { callWithError, collect } from '../lib/ipc'
import { useIpc } from '../lib/useIpc'
import { ScheduleForm } from './ScheduleForm'
import { describeCron, displayTime, scheduleState, type Preset, type Schedule } from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface SchedulesData {
  daemon_running?: boolean
  schedules?: Schedule[]
  presets?: Preset[]
}

type Note = { text: string; failed: boolean } | null

export function SchedulesPage () {
  const { data, loading, failed, reload } = useIpc<SchedulesData>('schedules')
  const [notes, setNotes] = useState<Record<string, Note>>({})
  /** 正在编辑的任务 id，'new' 表示新建。同时只能开一个 —— 两个表单并存会让人不确定
   *  保存的是哪个。 */
  const [editing, setEditing] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const note = useCallback((id: string, value: Note) => {
    setNotes((prev) => ({ ...prev, [id]: value }))
  }, [])

  /** 所有写操作的漏斗。写完强制重拉 —— 显示旧值等于说「没改成功」。 */
  const write = useCallback(async (key: string, method: string, params: Record<string, unknown>) => {
    setBusy(key)
    setError('')
    try {
      await callWithError(method, params)
      await reload()
      return true
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      return false
    } finally {
      setBusy('')
    }
  }, [reload])

  if (loading) return <p className="empty">{t('schedules.loading')}</p>
  if (failed || !data) return <p className="empty">{t('schedules.readFailed')}</p>

  const list = data.schedules || []
  const presets = data.presets || []
  const daemonOn = Boolean(data.daemon_running)

  const save = async (id: string, values: { name: string; action: string; cron: string }) => {
    const ok = id === 'new'
      ? await write('new', 'schedule_create', values)
      : await write(id, 'schedule_update', { id, ...values })
    if (ok) setEditing('')
  }

  return (
    <>
      <div className={daemonOn ? 'status-banner on' : 'status-banner'}>
        {daemonOn ? t('schedules.daemonOn') : t('schedules.daemonOff')}
      </div>

      {error ? <p className="sched-error">{error}</p> : null}

      <div className="sched-head">
        <button
          type="button"
          className="mbtn primary"
          disabled={Boolean(editing) || Boolean(busy)}
          onClick={() => { setEditing('new'); setError('') }}
          data-testid="schedule-new"
        >
          {t('schedules.newTask')}
        </button>
      </div>

      {editing === 'new' ? (
        <ScheduleForm
          busy={busy === 'new'}
          onSave={(values) => void save('new', values)}
          onCancel={() => setEditing('')}
        />
      ) : null}

      <div className="schedule-list">
        {list.length === 0 && editing !== 'new' ? (
          <p className="empty sched-empty">{t('schedules.noneYet')}</p>
        ) : null}

        {list.map((schedule, i) => {
          const id = schedule.id || String(i)
          return editing === id ? (
            <ScheduleForm
              key={id}
              schedule={schedule}
              busy={busy === id}
              onSave={(values) => void save(id, values)}
              onCancel={() => setEditing('')}
            />
          ) : (
            <ScheduleCard
              key={id}
              schedule={schedule}
              note={notes[id] || null}
              busy={busy === id}
              locked={Boolean(editing) || Boolean(busy)}
              onNote={(n) => note(id, n)}
              onEdit={() => { setEditing(id); setError('') }}
              onToggle={(enabled) => void write(id, 'schedule_toggle', { id, enabled })}
              onDelete={() => {
                // 删掉就没了，而且用户可能只是想临时停掉 —— 那有开关。
                if (window.confirm(t('schedules.deleteConfirm', { name: schedule.name || '' }))) {
                  void write(id, 'schedule_delete', { id })
                }
              }}
              onRan={reload}
            />
          )
        })}
      </div>

      {presets.length ? (
        <div className="sched-suggest">
          <div className="sched-suggest-h">
            <span className="sched-suggest-t">{t('schedules.suggestions')}</span>
            <span className="sched-suggest-s">{t('schedules.suggestionsHint')}</span>
          </div>
          {presets.map((preset) => (
            <div className="sched-grid sched-preset" key={preset.id}>
              <div className="schedule-name">{preset.name}</div>
              <div className="sched-preset-cadence">{describeCron(preset.cron)}</div>
              <div />
              <button
                type="button"
                className="mbtn"
                disabled={Boolean(editing) || Boolean(busy)}
                // 带上预置的 id，后端才能把它从推荐里剔掉。不传的话会拿到一个
                // 时间戳 id，推荐永远不消失、能重复添加。
                onClick={() => void write(preset.id, 'schedule_create', {
                  id: preset.id, name: preset.name, cron: preset.cron, action: preset.action
                })}
                data-preset={preset.id}
              >
                {busy === preset.id ? t('schedules.adding') : t('schedules.add')}
              </button>
            </div>
          ))}
        </div>
      ) : null}

    </>
  )
}

interface CardProps {
  schedule: Schedule
  note: Note
  busy: boolean
  locked: boolean
  onNote: (n: Note) => void
  onEdit: () => void
  onToggle: (enabled: boolean) => void
  onDelete: () => void
  onRan: () => void
}

function ScheduleCard (
  { schedule, note, busy, locked, onNote, onEdit, onToggle, onDelete, onRan }: CardProps
) {
  const [running, setRunning] = useState(false)
  const state = scheduleState(schedule)
  const enabled = Boolean(schedule.enabled)

  const lastValue = !enabled
    ? state.label
    : schedule.last_fired
      ? `${displayTime(schedule.last_fired)} · ${state.label}`
      : t('schedules.neverRun')

  const rerun = async () => {
    setRunning(true)
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
    setRunning(false)
    onRan()
  }

  return (
    <div className={enabled ? 'sched-grid schedule-card' : 'sched-grid schedule-card off'}>
      <div>
        <div className="schedule-name">{schedule.name || '—'}</div>
        <div className="schedule-cadence">{describeCron(schedule.cron)}</div>
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

      <div className="sched-ops">
        <button
          type="button"
          className={enabled ? 'sched-switch on' : 'sched-switch'}
          role="switch"
          aria-checked={enabled}
          disabled={busy || locked || !schedule.id}
          onClick={() => onToggle(!enabled)}
        >
          <span className="sched-knob" aria-hidden="true" />
          <span className="sched-switch-t">{enabled ? t('schedules.on') : t('schedules.off')}</span>
        </button>
        <button type="button" className="mbtn" disabled={busy || locked} onClick={onEdit}>
          {t('schedules.edit')}
        </button>
        <button type="button" className="mbtn danger" disabled={busy || locked} onClick={onDelete}>
          {t('schedules.delete')}
        </button>
        {/* 没有 id 的任务点了必然报错，直接禁用 */}
        <button
          type="button"
          className="schedule-rerun"
          disabled={running || busy || locked || !schedule.id}
          onClick={rerun}
        >
          {running ? t('schedules.rerunning') : (schedule.last_fired ? t('schedules.rerun') : t('schedules.runOnce'))}
        </button>
      </div>

      {note ? (
        <p className={note.failed ? 'schedule-rerun-note failed' : 'schedule-rerun-note'}>
          {note.text}
        </p>
      ) : null}
    </div>
  )
}
