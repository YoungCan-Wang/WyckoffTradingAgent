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
  /** 后端给的失败原因,空串表示没有具体原因。 */
  error: string
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
    // **必须把事件里的 userId 传下去。** 只调 invalidate() 会让 store 失去
    // 核对基准 —— 后端此刻若锁忙、返回上一个账号的持仓，就会被直接渲染。
    // 事件一直带着这个字段（App.tsx 派发时就放进去了），是这里丢掉了。
    //
    // 空串（退出登录）也要传:那是「应该看到未登录的持仓」，不是「不知道」。
    const onIdentityChange = (event: Event) => {
      const detail = (event as CustomEvent).detail as { userId?: string } | undefined
      invalidate(typeof detail?.userId === 'string' ? detail.userId : null)
    }
    window.addEventListener('wyckoff:account-changed', onIdentityChange)
    return () => window.removeEventListener('wyckoff:account-changed', onIdentityChange)
  }, [])

  return {
    portfolio: snap.portfolio,
    savedAt: snap.savedAt,
    loading: snap.loading,
    failed: snap.failed,
    error: snap.error,
    refresh
  }
}
