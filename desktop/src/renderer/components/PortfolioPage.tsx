/**
 * 持仓页：图表 + 可编辑的持仓表。
 *
 * 图表继续用 charts.js 的 renderCharts —— 它返回纯 DOM（SVG，不是 canvas），
 * 用 ref appendChild 挂进来就行。那 294 行 SVG 生成逻辑留到迁移的第 4 批再转 TS，
 * 免得一次改动同时动数据层和绘图层。
 */
import { useEffect, useRef, useState } from 'react'
import { callWithError } from '../lib/ipc'
import { usePortfolio } from '../lib/usePortfolio'
import { HoldingsTable } from './HoldingsTable'
import { AddPositionForm } from './AddPositionForm'
import type { Position } from '../types'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

const clock = (ms: number) => new Date(ms).toLocaleTimeString(window.WyckoffI18n.getLang())
const stamp = (raw?: string) => {
  if (!raw) return ''
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString(window.WyckoffI18n.getLang())
}

export function PortfolioPage () {
  const { portfolio, savedAt, loading, failed, refresh } = usePortfolio()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const chartsRef = useRef<HTMLDivElement | null>(null)

  // 图表由 vanilla 生成，所以每次数据变了都重挂一次。
  useEffect(() => {
    const host = chartsRef.current
    if (!host || !portfolio) return
    // withTable:false —— 下面那张可编辑的表才是唯一的持仓表。
    host.replaceChildren(window.WyckoffCharts.renderCharts(portfolio, { withTable: false }))
  }, [portfolio])

  /**
   * 写入统一入口：失败原样显示后端消息，成功后强制重拉。
   *
   * 显示完还要把异常抛出去 —— 调用方需要知道成功与否才能决定后续动作
   * （比如「建仓成功才继续设止损」、「成功才清空表单」）。只 setError 不抛，
   * 失败后会接着做下一步，看起来像部分成功。
   */
  const write = async (key: string, method: string, params: Record<string, unknown>) => {
    setBusy(key)
    setError('')
    try {
      await callWithError(method, params)
      // 写完必须重拉：缓存此刻一定是脏的，显示旧值等于说「没改成功」。
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      throw err
    } finally {
      setBusy('')
    }
  }

  if (loading) return <p className="empty">{t('tab.loading')}</p>
  if (failed || !portfolio) return <p className="empty">{t('portfolio.readFailed')}</p>

  const positions = portfolio.positions || []

  return (
    <>
      <div className="pbar">
        <span className="pbar-t">
          {savedAt ? t('portfolio.updatedAt', { time: clock(savedAt) }) : t('portfolio.justLoaded')}
          {portfolio.valuation_updated_at
            ? ` · ${t('portfolio.valuedAt', { time: stamp(portfolio.valuation_updated_at) })}`
            : ''}
        </span>
        <button type="button" className="mbtn" disabled={Boolean(busy)} onClick={() => void refresh()}>
          {t('portfolio.refresh')}
        </button>
      </div>

      {error ? <p className="pbar-err">{error}</p> : null}

      <div ref={chartsRef} />

      {positions.length ? (
        <HoldingsTable
          positions={positions}
          busy={busy}
          onUpdate={(code, shares, costPrice) =>
            write(code, 'portfolio_edit', { action: 'update', code, shares, cost_price: costPrice })}
          onSetStop={(code, stop) =>
            write(code, 'portfolio_set_stop', { code, stop_loss: stop })}
          onRemove={async (position: Position) => {
            const label = `${position.code} ${position.name || ''}`.trim()
            if (!window.confirm(t('portfolio.removeConfirm', { name: label }))) return
            await write(position.code, 'portfolio_edit', { action: 'remove', code: position.code })
          }}
          onOpenChart={(code) => window.WyckoffOpenKline && window.WyckoffOpenKline(code)}
        />
      ) : null}

      <AddPositionForm
        busy={busy === '__add__'}
        onAdd={async ({ stop_loss: stop, ...fields }) => {
          // update_portfolio 的 add 不接受 stop_loss —— 直接连着传会被静默丢掉，
          // 用户以为止损设上了。所以先建仓，再单独设一次止损。
          await write('__add__', 'portfolio_edit', { action: 'add', ...fields })
          if (typeof stop === 'number') {
            await write('__add__', 'portfolio_set_stop', { code: fields.code, stop_loss: stop })
          }
        }}
      />

      <CashRow
        value={portfolio.free_cash}
        busy={busy === '__cash__'}
        onSave={(free_cash) => write('__cash__', 'portfolio_edit', { action: 'set_cash', free_cash })}
      />
    </>
  )
}

function CashRow ({ value, busy, onSave }: {
  value: number
  busy: boolean
  onSave: (value: number) => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(String(value ?? 0))
  const [error, setError] = useState('')

  const submit = async () => {
    const next = Number(draft)
    // 现金可以是 0（满仓），所以只拦负数 —— 与后端的 >= 0 一致。
    if (!Number.isFinite(next) || next < 0) { setError(t('portfolio.badCash')); return }
    setError('')
    await onSave(next)
    setEditing(false)
  }

  return (
    <div className="srow pcash">
      <span className="slab">{t('charts.kpiCash')}</span>
      {editing ? (
        <>
          <input className="pin" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button type="button" className="mbtn" disabled={busy} onClick={() => void submit()}>
            {busy ? t('portfolio.saving') : t('action.save')}
          </button>
          <button type="button" className="mbtn" disabled={busy} onClick={() => setEditing(false)}>
            {t('portfolio.cancel')}
          </button>
          {error ? <span className="snote err">{error}</span> : null}
        </>
      ) : (
        <>
          <b>{Number(value ?? 0).toFixed(2)}</b>
          <button
            type="button"
            className="mbtn"
            onClick={() => { setDraft(String(value ?? 0)); setEditing(true) }}
          >
            {t('portfolio.edit')}
          </button>
        </>
      )}
    </div>
  )
}
