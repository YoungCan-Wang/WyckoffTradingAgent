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
const runHandled = (result: Promise<void>) => { void result.catch(() => {}) }

const clock = (ms: number) => new Date(ms).toLocaleTimeString(window.WyckoffI18n.getLang())
const stamp = (raw?: string) => {
  if (!raw) return ''
  const date = new Date(raw)
  return Number.isNaN(date.getTime()) ? raw : date.toLocaleString(window.WyckoffI18n.getLang())
}

export function PortfolioPage () {
  const { portfolio, savedAt, loading, failed, error: loadError, refresh } = usePortfolio()
  const [busy, setBusy] = useState('')
  // 编辑模式：整页一个开关，比每行两个按钮安静得多。
  const [editing, setEditing] = useState(false)
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
  // 读失败要给出「怎么办」,不只是「不行」。
  //
  // 原来这里只有一句 readFailed,而更糟的是失败常常根本走不到这里:后端云端
  // 超时会返回 `{portfolio: {error: ...}}`,那是个 truthy 对象,于是被当成
  // 一份合法持仓渲染,图表画出「暂无持仓数据」—— 网络抖一下看起来就像清仓了。
  // store 现在把它识别成失败(见 portfolioStore.refresh),这里负责说清楚
  // 原因并给一个能真正解决问题的按钮。
  if (failed || !portfolio) {
    return (
      <div className="pfail">
        <p className="empty">{t('portfolio.readFailed')}</p>
        {loadError ? <p className="pfail-why">{loadError}</p> : null}
        <p className="pfail-hint">{t('portfolio.retryHint')}</p>
        <button type="button" className="mbtn" onClick={() => void refresh()}>
          {t('portfolio.refresh')}
        </button>
      </div>
    )
  }

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
        {/* 编辑是一个模式开关，不是每行两个按钮 —— 那样按钮会把真正的内容挤到中间。 */}
        <button
          type="button"
          className={editing ? 'mbtn on' : 'mbtn'}
          disabled={Boolean(busy)}
          onClick={() => { setEditing(!editing); setError('') }}
        >
          {editing ? t('portfolio.doneEditing') : t('portfolio.startEditing')}
        </button>
      </div>

      {editing ? <p className="pbar-hint">{t('portfolio.editHint')}</p> : null}

      {/* 降级提示：云端没连上、这份是本地数据。
          必须说出来 —— 本地库可能落后于另一台设备上的改动,而用户看不出差别,
          会以为自己在看最新状态。 */}
      {portfolio.source === 'local' ? (
        <p className="pbar-warn">
          {t('portfolio.localFallback')}
          {portfolio.cloud_error ? ` (${portfolio.cloud_error})` : ''}
        </p>
      ) : null}

      {error ? <p className="pbar-err">{error}</p> : null}

      <div ref={chartsRef} />

      {positions.length ? (
        <HoldingsTable
          positions={positions}
          busy={busy}
          editing={editing}
          onError={setError}
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

      {/* 添加与改现金也只在编辑模式出现：平时看持仓不需要它们占位置。 */}
      {editing ? (
        <>
          <AddPositionForm
            busy={busy === '__add__'}
            onAdd={async ({ stop_loss: stop, ...fields }) => {
              // update_portfolio 的 add 不接受 stop_loss —— 直接连着传会被静默
              // 丢掉，用户以为止损设上了。所以先建仓，再单独设一次止损。
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
            onError={setError}
          />
        </>
      ) : null}
    </>
  )
}

/**
 * 现金一行。已经在编辑模式里了，所以直接就是输入框 —— 再套一层「编辑/保存/
 * 取消」是把同一个开关做两遍。失焦即保存，与持仓行一致。
 */
function CashRow ({ value, busy, onSave, onError }: {
  value: number
  busy: boolean
  onSave: (value: number) => Promise<void>
  onError: (message: string) => void
}) {
  const [draft, setDraft] = useState(String(value ?? 0))

  // 保存后重拉会带来新值，同步回输入框。
  useEffect(() => { setDraft(String(value ?? 0)) }, [value])

  const commit = async () => {
    // 空串必须先拦掉：Number('') === 0，会通过下面的「有限且 >= 0」检查，然后
    // 把现金**静默写成 ¥0**。而这一行是失焦即存的，所以「全选删除准备重输 ->
    // 点了别处」这个完全正常的操作就会清空账户现金。
    //
    // 空输入的语义是「我还没填」，不是「零」。想记 0 的人会真的打一个 0。
    // 持仓行因为 shares <= 0 被拦住，唯独现金允许 0，于是漏在这里。
    if (!draft.trim()) {
      setDraft(String(value ?? 0))
      return
    }
    const next = Number(draft)
    if (next === value) return
    // 现金可以是 0（满仓），只拦负数 —— 与后端的 >= 0 一致。
    if (!Number.isFinite(next) || next < 0) {
      setDraft(String(value ?? 0))
      onError(t('portfolio.badCash'))
      return
    }
    await onSave(next)
  }

  return (
    <div className="srow pcash">
      <span className="slab">{t('charts.kpiCash')}</span>
      <input
        className="pin"
        value={draft}
        disabled={busy}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => runHandled(commit())}
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return
          e.preventDefault()
          runHandled(commit())
        }}
      />
    </div>
  )
}
