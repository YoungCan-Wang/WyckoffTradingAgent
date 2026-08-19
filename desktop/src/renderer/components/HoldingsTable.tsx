/**
 * 可编辑的持仓表。
 *
 * 替代 charts.js 里的只读 holdingsTable —— 那张表的行是可点的（打开 K 线），
 * 所以行内按钮必须 stopPropagation，否则点「删」会同时弹出 K 线图。
 */
import { useState } from 'react'
import type { Position } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)
const DASH = '—'

const num = (value: number | null | undefined, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : DASH

interface Props {
  positions: Position[]
  busy: string
  /** 改股数/成本。空字符串的止损由 onSetStop 单独处理。 */
  onUpdate: (code: string, shares: number, costPrice: number) => Promise<void>
  onSetStop: (code: string, stop: number | null) => Promise<void>
  onRemove: (position: Position) => Promise<void>
  onOpenChart: (code: string) => void
}

export function HoldingsTable ({ positions, busy, onUpdate, onSetStop, onRemove, onOpenChart }: Props) {
  const [editing, setEditing] = useState<string | null>(null)

  return (
    <div className="dtbl">
      <table>
        <tbody>
          <tr>
            <th>{t('charts.colCode')}</th>
            <th>{t('charts.colName')}</th>
            <th>{t('charts.colShares')}</th>
            <th>{t('charts.colCost')}</th>
            <th>{t('charts.colStop')}</th>
            <th />
          </tr>
          {positions.map((position) =>
            editing === position.code ? (
              <EditRow
                key={position.code}
                position={position}
                busy={busy === position.code}
                onCancel={() => setEditing(null)}
                onSave={async (shares, cost, stop) => {
                  await onUpdate(position.code, shares, cost)
                  // 止损单独一条通道（后端的窄接口），只在真的变了时才发。
                  if (stop !== position.stop_loss) await onSetStop(position.code, stop)
                  setEditing(null)
                }}
              />
            ) : (
              <ViewRow
                key={position.code}
                position={position}
                busy={busy === position.code}
                onEdit={() => setEditing(position.code)}
                onRemove={() => void onRemove(position)}
                onOpenChart={() => onOpenChart(position.code)}
              />
            )
          )}
        </tbody>
      </table>
    </div>
  )
}

function ViewRow ({ position, busy, onEdit, onRemove, onOpenChart }: {
  position: Position
  busy: boolean
  onEdit: () => void
  onRemove: () => void
  onOpenChart: () => void
}) {
  return (
    <tr className="dtbl-row" title={t('charts.openChart')} onClick={onOpenChart}>
      <td className="c">{position.code}</td>
      <td>{position.name || DASH}</td>
      <td>{position.shares}</td>
      <td>{num(position.cost_price)}</td>
      {/* 没设止损时标红，与原表一致 */}
      <td className={position.stop_loss === null ? 'warnc' : undefined}>{num(position.stop_loss)}</td>
      <td className="pacts">
        {/* stopPropagation：不然点按钮会连带触发整行的「打开 K 线」 */}
        <button
          type="button"
          className="mbtn"
          disabled={busy}
          onClick={(e) => { e.stopPropagation(); onEdit() }}
        >
          {t('portfolio.edit')}
        </button>
        <button
          type="button"
          className="mbtn danger"
          disabled={busy}
          onClick={(e) => { e.stopPropagation(); onRemove() }}
        >
          {t('portfolio.remove')}
        </button>
      </td>
    </tr>
  )
}

function EditRow ({ position, busy, onCancel, onSave }: {
  position: Position
  busy: boolean
  onCancel: () => void
  onSave: (shares: number, costPrice: number, stop: number | null) => Promise<void>
}) {
  const [shares, setShares] = useState(String(position.shares))
  const [cost, setCost] = useState(String(position.cost_price))
  // 空字符串代表「清除止损」——后端现在支持 null 了。
  const [stop, setStop] = useState(position.stop_loss === null ? '' : String(position.stop_loss))
  const [error, setError] = useState('')

  const submit = async () => {
    const s = Number(shares)
    const c = Number(cost)
    // 与后端同规则的预校验，只为即时反馈；后端仍会自己再校验一遍。
    if (!Number.isFinite(s) || s <= 0) { setError(t('portfolio.badShares')); return }
    if (!Number.isFinite(c) || c <= 0) { setError(t('portfolio.badCost')); return }
    const trimmed = stop.trim()
    let nextStop: number | null = null
    if (trimmed) {
      const parsed = Number(trimmed)
      // 0 不是「清除」，是无效价格 —— 想清除就把框留空。
      if (!Number.isFinite(parsed) || parsed <= 0) { setError(t('portfolio.badStop')); return }
      nextStop = parsed
    }
    setError('')
    await onSave(s, c, nextStop)
  }

  return (
    <tr className="pedit">
      <td className="c">{position.code}</td>
      <td>{position.name || DASH}</td>
      <td><input className="pin" value={shares} onChange={(e) => setShares(e.target.value)} /></td>
      <td><input className="pin" value={cost} onChange={(e) => setCost(e.target.value)} /></td>
      <td>
        <input
          className="pin"
          value={stop}
          placeholder={t('portfolio.stopPlaceholder')}
          onChange={(e) => setStop(e.target.value)}
        />
      </td>
      <td className="pacts">
        <button type="button" className="mbtn" disabled={busy} onClick={() => void submit()}>
          {busy ? t('portfolio.saving') : t('action.save')}
        </button>
        <button type="button" className="mbtn" disabled={busy} onClick={onCancel}>
          {t('portfolio.cancel')}
        </button>
        {error ? <span className="snote err">{error}</span> : null}
      </td>
    </tr>
  )
}
