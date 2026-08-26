/**
 * 添加持仓。默认折叠 —— 加仓是偶发操作，表格才是主体。
 *
 * 复用 AddModelForm 那套 .mform / .mfield 类名，不引入新的弹窗原语。
 */
import { useState } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

export interface NewPosition {
  code: string
  name: string
  shares: number
  cost_price: number
  buy_dt: string
  stop_loss?: number | null
}

const EMPTY = { code: '', name: '', shares: '', cost_price: '', buy_dt: '', stop_loss: '' }

/** 默认填今天：add 要求 buy_dt，让用户手打日期是没必要的摩擦。 */
function today (): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

export function AddPositionForm ({ busy, onAdd }: {
  busy: boolean
  onAdd: (fields: NewPosition) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [fields, setFields] = useState({ ...EMPTY, buy_dt: today() })
  const [error, setError] = useState('')

  const set = (key: keyof typeof EMPTY) => (value: string) =>
    setFields((prev) => ({ ...prev, [key]: value }))

  const submit = async () => {
    const shares = Number(fields.shares)
    const cost = Number(fields.cost_price)
    // 预校验只为即时反馈，后端仍会自己校验一遍（代码格式、日期、是否已存在）。
    if (!fields.code.trim()) { setError(t('portfolio.badCode')); return }
    if (!Number.isFinite(shares) || shares <= 0) { setError(t('portfolio.badShares')); return }
    if (!Number.isFinite(cost) || cost <= 0) { setError(t('portfolio.badCost')); return }
    if (!fields.buy_dt.trim()) { setError(t('portfolio.badDate')); return }

    const stopRaw = fields.stop_loss.trim()
    let stop: number | null = null
    if (stopRaw) {
      const parsed = Number(stopRaw)
      if (!Number.isFinite(parsed) || parsed <= 0) { setError(t('portfolio.badStop')); return }
      stop = parsed
    }

    setError('')
    try {
      await onAdd({
        code: fields.code.trim(),
        name: fields.name.trim(),
        shares,
        cost_price: cost,
        buy_dt: fields.buy_dt.trim(),
        stop_loss: stop
      })
      // 成功才清空并收起；失败要留着让用户改，不然填的东西全没了。
      setFields({ ...EMPTY, buy_dt: today() })
      setOpen(false)
    } catch {
      /* 错误由父组件统一显示 */
    }
  }

  return (
    <div className="madd">
      <button type="button" className="wel-c" onClick={() => setOpen(!open)}>
        {open ? t('portfolio.cancelAdd') : t('portfolio.addPosition')}
      </button>
      {open ? (
        <div className="mform">
          <Field label={t('charts.colCode')} placeholder="600519 / 00700.HK / AAPL.US"
            value={fields.code} onChange={set('code')} />
          <Field label={t('charts.colName')} placeholder={t('portfolio.namePlaceholder')}
            value={fields.name} onChange={set('name')} />
          <Field label={t('charts.colShares')} placeholder="100"
            value={fields.shares} onChange={set('shares')} />
          <Field label={t('charts.colCost')} placeholder="1680.50"
            value={fields.cost_price} onChange={set('cost_price')} />
          <Field label={t('portfolio.buyDate')} placeholder="2026-08-19"
            value={fields.buy_dt} onChange={set('buy_dt')} />
          <Field label={t('charts.colStop')} placeholder={t('portfolio.stopOptional')}
            value={fields.stop_loss} onChange={set('stop_loss')} />
          <button type="button" className="wel-c" disabled={busy} onClick={() => void submit()}>
            {busy ? t('portfolio.saving') : t('action.save')}
          </button>
          {error ? <span className="snote err">{error}</span> : null}
        </div>
      ) : null}
    </div>
  )
}

function Field ({ label, placeholder, value, onChange }: {
  label: string
  placeholder: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="mfield">
      <label className="mflab">{label}</label>
      <input className="mfin" placeholder={placeholder} value={value}
        onChange={(e) => onChange(e.target.value)} />
    </div>
  )
}
