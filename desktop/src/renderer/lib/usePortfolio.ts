/**
 * 持仓数据 + 缓存策略。
 *
 * 默认不自动重拉：有缓存就用缓存。刷新是手动的，但写入之后必须强制重拉 ——
 * 那时缓存一定是脏的，显示旧值等于告诉用户「没改成功」。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { collect } from './ipc'
import { readCache, writeCache } from './portfolioCache'
import type { Portfolio } from '../types'

export interface PortfolioState {
  portfolio: Portfolio | null
  /** 缓存写入时间；null 表示这份数据是刚拉的。 */
  savedAt: number | null
  loading: boolean
  failed: boolean
  /** 手动刷新，或写入后强制重拉。 */
  refresh: () => Promise<void>
}

export function usePortfolio (): PortfolioState {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [savedAt, setSavedAt] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  // 卸载后不再 setState：页面切换比请求返回快时会警告。
  const alive = useRef(true)

  const fetchFresh = useCallback(async () => {
    setLoading(true)
    setFailed(false)
    const res = await collect('portfolio').catch(() => null)
    if (!alive.current) return
    const next = res && (res as { portfolio?: Portfolio }).portfolio
    if (!next) {
      setFailed(true)
      setLoading(false)
      return
    }
    const entry = writeCache(next)
    setPortfolio(next)
    setSavedAt(entry.savedAt)
    setLoading(false)
  }, [])

  useEffect(() => {
    alive.current = true
    const cached = readCache()
    if (cached) {
      // 命中缓存就直接显示，不发 IPC —— 这正是「不要每次都重拉」。
      setPortfolio(cached.portfolio)
      setSavedAt(cached.savedAt)
      setLoading(false)
    } else {
      void fetchFresh()
    }
    return () => { alive.current = false }
  }, [fetchFresh])

  return { portfolio, savedAt, loading, failed, refresh: fetchFresh }
}
