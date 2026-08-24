/**
 * 持仓数据的 React 视图。
 *
 * 这里**不再持有数据** —— 数据在 portfolioStore 里,这个 hook 只是订阅它。
 * 原来每个组件各自 fetch 各自存,首页和持仓页因此看到两份不同的持仓
 * (首页永远是开机那一刻的,也就是 0)。理由详见 portfolioStore.ts 顶部。
 *
 * 用 `useSyncExternalStore`:React 18 为「外部 store」提供的官方原语,
 * 自带 tearing 保护,比 useState + 事件监听手写一遍更可靠。
 */
import { useEffect, useSyncExternalStore } from 'react'
import { ensureLoaded, getSnapshot, invalidate, refresh, subscribe } from './portfolioStore'
import type { Portfolio } from '../types'

export interface PortfolioState {
  portfolio: Portfolio | null
  /** 缓存写入时间;null 表示这份数据是刚拉的。 */
  savedAt: number | null
  loading: boolean
  failed: boolean
  /** 手动刷新,或写入后强制重拉。 */
  refresh: () => Promise<void>
}

export function usePortfolio (): PortfolioState {
  const snap = useSyncExternalStore(subscribe, getSnapshot)

  // 首个订阅者负责触发加载。store 内部幂等,多个组件同时挂载只会拉一次。
  useEffect(() => { void ensureLoaded() }, [])

  // 登录态变化 → 作废并重拉。监听放在 hook 里而不是 store 模块顶层:
  // 模块顶层注册的监听器永远不会解绑,测试之间会互相污染。
  useEffect(() => {
    const onIdentityChange = () => invalidate()
    window.addEventListener('wyckoff:account-changed', onIdentityChange)
    return () => window.removeEventListener('wyckoff:account-changed', onIdentityChange)
  }, [])

  return {
    portfolio: snap.portfolio,
    savedAt: snap.savedAt,
    loading: snap.loading,
    failed: snap.failed,
    refresh
  }
}
