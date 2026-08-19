/**
 * 持仓表。只读时就是一张干净的表；进入编辑模式后每行变成输入框。
 *
 * 为什么不是每行两个按钮：那样一屏就是十几个「改」「删」，真正的内容（代码、
 * 盈亏）被按钮挤在中间。编辑是偶发动作，收到顶部一个开关里，平时不占视觉。
 *
 * 行本身是可点的（打开 K 线），所以编辑态里的输入框和删除键都要
 * stopPropagation，否则改个数字会顺带弹出 K 线图。
 */
import { useEffect, useState } from 'react'
import type { Position } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)
const DASH = '—'

const num = (value: number | null | undefined, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : DASH

interface Props {
  positions: Position[]
  /** 正在写入的那一行的代码；空串表示空闲。 */
  busy: string
  editing: boolean
  onUpdate: (code: string, shares: number, costPrice: number) => Promise<void>
  onSetStop: (code: string, stop: number | null) => Promise<void>
  onRemove: (position: Position) => Promise<void>
  onOpenChart: (code: string) => void
  onError: (message: string) => void
}

export function HoldingsTable (props: Props) {
  const { positions, editing, onOpenChart } = props
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
            {editing ? <th /> : null}
          </tr>
          {positions.map((position) =>
            editing ? (
              <EditRow key={position.code} position={position} {...props} />
            ) : (
              <tr
                key={position.code}
                className="dtbl-row"
                title={t('charts.openChart')}
                onClick={() => onOpenChart(position.code)}
              >
                <td className="c">{position.code}</td>
                <td>{position.name || DASH}</td>
                <td>{position.shares}</td>
                <td>{num(position.cost_price)}</td>
                {/* 没设止损标红，与迁移前一致 */}
                <td className={position.stop_loss === null ? 'warnc' : undefined}>
                  {num(position.stop_loss)}
                </td>
              </tr>
            )
          )}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 编辑态的一行。失焦即保存 —— 每行再放一个「保存」按钮就回到了按钮太多的老问题。
 */
function EditRow ({ position, busy, onUpdate, onSetStop, onRemove, onError }: Props & { position: Position }) {
  const [shares, setShares] = useState(String(position.shares))
  const [cost, setCost] = useState(String(position.cost_price))
  // 空字符串代表「清除止损」。
  const [stop, setStop] = useState(position.stop_loss === null ? '' : String(position.stop_loss))

  // 外部数据变了（比如刚保存完重拉）要同步回输入框，否则显示的是旧草稿。
  useEffect(() => {
    setShares(String(position.shares))
    setCost(String(position.cost_price))
    setStop(position.stop_loss === null ? '' : String(position.stop_loss))
  }, [position.shares, position.cost_price, position.stop_loss])

  const saving = busy === position.code

  const commitAmounts = async () => {
    const s = Number(shares)
    const c = Number(cost)
    if (s === position.shares && c === position.cost_price) return
    // 与后端同规则的预校验，只为即时反馈；后端仍会自己校验。
    if (!Number.isFinite(s) || s <= 0) { setShares(String(position.shares)); onError(t('portfolio.badShares')); return }
    if (!Number.isFinite(c) || c <= 0) { setCost(String(position.cost_price)); onError(t('portfolio.badCost')); return }
    await onUpdate(position.code, s, c)
  }

  const commitStop = async () => {
    const trimmed = stop.trim()
    const next = trimmed === '' ? null : Number(trimmed)
    if (next === position.stop_loss) return
    // 0 不是「清除」，是无效价格；想清除就把框留空。
    if (next !== null && (!Number.isFinite(next) || next <= 0)) {
      setStop(position.stop_loss === null ? '' : String(position.stop_loss))
      onError(t('portfolio.badStop'))
      return
    }
    await onSetStop(position.code, next)
  }

  return (
    <tr className={saving ? 'pedit saving' : 'pedit'}>
      <td className="c">{position.code}</td>
      <td>{position.name || DASH}</td>
      <td>
        <Cell value={shares} onChange={setShares} onCommit={commitAmounts} disabled={saving} />
      </td>
      <td>
        <Cell value={cost} onChange={setCost} onCommit={commitAmounts} disabled={saving} />
      </td>
      <td>
        <Cell
          value={stop}
          onChange={setStop}
          onCommit={commitStop}
          disabled={saving}
          placeholder={t('portfolio.stopPlaceholder')}
        />
      </td>
      <td>
        <button
          type="button"
          className="prm"
          disabled={saving}
          title={t('portfolio.remove')}
          aria-label={t('portfolio.remove')}
          onClick={(e) => { e.stopPropagation(); void onRemove(position) }}
        >
          ×
        </button>
      </td>
    </tr>
  )
}

function Cell ({ value, onChange, onCommit, disabled, placeholder }: {
  value: string
  onChange: (v: string) => void
  onCommit: () => void | Promise<void>
  disabled: boolean
  placeholder?: string
}) {
  return (
    <input
      className="pin"
      value={value}
      disabled={disabled}
      placeholder={placeholder}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => onChange(e.target.value)}
      onBlur={() => void onCommit()}
      // 回车即提交：改完一格直接按回车比找鼠标快。
      //
      // 直接调 onCommit 而不是 .blur()：靠 blur 间接触发要依赖焦点真的移走，
      // 而且随后的失焦还会再提交一次（第二次是空操作，但多一次往返）。
      onKeyDown={(e) => {
        if (e.key !== 'Enter') return
        e.preventDefault()
        void onCommit()
      }}
    />
  )
}
