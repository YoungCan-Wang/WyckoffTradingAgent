/**
 * 定时任务的编辑表单。新建和编辑共用。
 *
 * **界面上不出现 cron 表达式。** 用户没有那个能力，也不该被要求有 —— 重复方式和
 * 时间都用下拉框，cron 只是存储格式。表单底部实时显示一句人话（「工作日 09:25」），
 * 让人在保存前就确认自己选对了。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  buildCron, describeCron, parseCron, weekdayName,
  DEFAULT_CADENCE, type Cadence, type Repeat, type Schedule
} from '../lib/schedules'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  /** 传了就是编辑，没传就是新建 */
  schedule?: Schedule
  busy: boolean
  onSave: (values: { name: string; action: string; cron: string }) => void
  onCancel: () => void
}

const HOURS = Array.from({ length: 24 }, (_, i) => i)
const MINUTES = Array.from({ length: 12 }, (_, i) => i * 5)
const MONTHDAYS = Array.from({ length: 31 }, (_, i) => i + 1)
const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0]

export function ScheduleForm ({ schedule, busy, onSave, onCancel }: Props) {
  const [name, setName] = useState(schedule?.name || '')
  const [action, setAction] = useState(schedule?.action || '')
  // 编辑一个 cron 认不出的老任务时 parseCron 返回 null。那时不能悄悄用默认值 ——
  // 用户点开编辑、直接保存，触发时间就被换掉了。所以留住这个信息并提示。
  const parsed = useMemo(() => (schedule?.cron ? parseCron(schedule.cron) : null), [schedule?.cron])
  const unparseable = Boolean(schedule?.cron && !parsed)
  const [cadence, setCadence] = useState<Cadence>(parsed || DEFAULT_CADENCE)

  // 切到另一个任务时表单要跟着换。不重置的话会把上一个任务的值保存到这一个身上。
  useEffect(() => {
    setName(schedule?.name || '')
    setAction(schedule?.action || '')
    setCadence(parsed || DEFAULT_CADENCE)
  }, [schedule?.id, schedule?.name, schedule?.action, parsed])

  const cron = buildCron(cadence)
  const patch = (next: Partial<Cadence>) => setCadence((prev) => ({ ...prev, ...next }))
  const canSave = Boolean(name.trim() && action.trim()) && !busy

  return (
    <form
      className="sf"
      onSubmit={(e) => {
        e.preventDefault()
        if (canSave) onSave({ name: name.trim(), action: action.trim(), cron })
      }}
    >
      <label className="sf-row">
        <span className="sf-label">{t('schedules.fieldName')}</span>
        <input
          className="sf-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={60}
          autoFocus
        />
      </label>

      <label className="sf-row">
        <span className="sf-label">{t('schedules.fieldAction')}</span>
        <span className="sf-stack">
          <textarea
            className="sf-input sf-textarea"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            rows={2}
            maxLength={2000}
          />
          <span className="sf-hint">{t('schedules.fieldActionHint')}</span>
        </span>
      </label>

      <div className="sf-row">
        <span className="sf-label">{t('schedules.fieldRepeat')}</span>
        <span className="sf-controls">
          <select
            className="sf-select"
            value={cadence.repeat}
            onChange={(e) => patch({ repeat: e.target.value as Repeat })}
            aria-label={t('schedules.fieldRepeat')}
          >
            <option value="weekday">{t('schedules.repeatWeekday')}</option>
            <option value="weekly">{t('schedules.repeatWeekly')}</option>
            <option value="monthly">{t('schedules.repeatMonthly')}</option>
          </select>

          {cadence.repeat === 'weekly' ? (
            <select
              className="sf-select"
              value={cadence.weekday}
              onChange={(e) => patch({ weekday: Number(e.target.value) })}
              aria-label={t('schedules.repeatWeekly')}
            >
              {WEEKDAYS.map((d) => (
                <option key={d} value={d}>{weekdayName(d)}</option>
              ))}
            </select>
          ) : null}

          {cadence.repeat === 'monthly' ? (
            <select
              className="sf-select"
              value={cadence.monthday}
              onChange={(e) => patch({ monthday: Number(e.target.value) })}
              aria-label={t('schedules.repeatMonthly')}
            >
              {MONTHDAYS.map((d) => (
                <option key={d} value={d}>{d}{t('schedules.monthdayUnit')}</option>
              ))}
            </select>
          ) : null}
        </span>
      </div>

      <div className="sf-row">
        <span className="sf-label">{t('schedules.fieldTime')}</span>
        <span className="sf-controls">
          <select
            className="sf-select tnum"
            value={cadence.hour}
            onChange={(e) => patch({ hour: Number(e.target.value) })}
            aria-label={t('schedules.fieldTime')}
          >
            {HOURS.map((h) => (
              <option key={h} value={h}>{String(h).padStart(2, '0')}</option>
            ))}
          </select>
          <span className="sf-colon">:</span>
          <select
            className="sf-select tnum"
            value={cadence.minute}
            onChange={(e) => patch({ minute: Number(e.target.value) })}
            aria-label={t('schedules.fieldTime')}
          >
            {/* 5 分钟一档。定时任务不需要精确到分钟，60 个选项翻起来更累 ——
                但预置的 09:25 / 15:05 都在档上。 */}
            {MINUTES.map((m) => (
              <option key={m} value={m}>{String(m).padStart(2, '0')}</option>
            ))}
          </select>
        </span>
      </div>

      <div className="sf-row">
        <span className="sf-label" />
        <span className="sf-stack">
          <span className="sf-preview">{t('schedules.willRun', { desc: describeCron(cron) })}</span>
          {cadence.repeat === 'monthly' && cadence.monthday > 28 ? (
            // 29-31 号会跳过短月。用户选了 31 号却发现 2 月没跑，会以为坏了。
            <span className="sf-hint">{t('schedules.monthdaySkipNote', { day: cadence.monthday })}</span>
          ) : null}
          {unparseable ? (
            <span className="sf-hint sf-warn">{t('schedules.notEditableHere')}</span>
          ) : null}
        </span>
      </div>

      <div className="sf-actions">
        <button type="submit" className="mbtn primary" disabled={!canSave}>
          {t('schedules.save')}
        </button>
        <button type="button" className="mbtn" onClick={onCancel} disabled={busy}>
          {t('schedules.cancel')}
        </button>
      </div>
    </form>
  )
}
