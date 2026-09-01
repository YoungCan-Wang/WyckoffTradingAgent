/**
 * 聊天里的威科夫图。
 *
 * 图画在浏览器端,不走 matplotlib —— 沙箱里没网装不了包,也读不回图片字节。
 * 顺带把中文字体那一整套约束消掉:浏览器渲染中文本来就不会 fallback 成方框。
 *
 * K 线不在工具返回值里(带 320 根进上下文太贵),所以这里按需自己取,并且只在
 * 用户展开时才发请求。
 */

import { useEffect, useState } from 'react'
import type { WyckoffChartPlan, KlineRow } from '@wyckoff/shared'
import { KlineChart } from '@/components/kline-chart'
import { fetchKlineWithQuality, getUserDataKeys } from '@/lib/kline'
import { useAuthStore } from '@/stores/auth'

type LoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; rows: KlineRow[] }
  | { status: 'error'; message: string }

export function WyckoffChartPanel({ code, plan }: { code: string; plan: WyckoffChartPlan }) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<LoadState>({ status: 'idle' })
  const userId = useAuthStore((store) => store.user?.id)

  useEffect(() => {
    if (!open || state.status !== 'idle' || !userId) return
    let cancelled = false
    setState({ status: 'loading' })
    void (async () => {
      try {
        const keys = await getUserDataKeys(userId)
        const result = await fetchKlineWithQuality(code, keys, userId)
        if (!cancelled) setState({ status: 'ready', rows: result.data })
      } catch (error) {
        if (!cancelled) setState({ status: 'error', message: error instanceof Error ? error.message : String(error) })
      }
    })()
    return () => { cancelled = true }
  }, [open, state.status, code, userId])

  return (
    <div className="rounded-md border border-border/50 bg-muted/20 px-2 py-1.5 text-[11px]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full cursor-pointer text-left font-medium text-foreground"
      >
        {open ? '收起威科夫结构图' : '展开威科夫结构图'}
      </button>
      {open && (
        <div className="mt-2">
          {state.status === 'loading' && <p className="text-muted-foreground">正在取 K 线…</p>}
          {state.status === 'error' && <p className="text-amber-700 dark:text-amber-200">取 K 线失败：{state.message}</p>}
          {!userId && <p className="text-muted-foreground">未登录，无法取 K 线。</p>}
          {state.status === 'ready' && (
            state.rows.length > 0
              ? <KlineChart data={state.rows} height={380} chartPlan={plan} showIndicators />
              : <p className="text-muted-foreground">没有可用 K 线，不画图。</p>
          )}
        </div>
      )}
    </div>
  )
}
