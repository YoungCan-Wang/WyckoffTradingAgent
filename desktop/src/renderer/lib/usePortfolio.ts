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

  /**
   * 当前账号。缓存按它分区，所以读缓存之前必须先知道「我是谁」——
   * 先读缓存再问账号的话，A 退出换 B 登录时 B 会看到 A 的持仓，而且因为命中
   * 缓存不发请求，这个错永远不会被纠正。
   */
  const currentUser = useCallback(async (): Promise<string> => {
    const res = await collect('account').catch(() => null)
    return String((res as { user_id?: string } | null)?.user_id || '')
  }, [])

  const fetchFresh = useCallback(async () => {
    setLoading(true)
    setFailed(false)
    const uid = await currentUser()
    if (!alive.current) return
    const res = await collect('portfolio').catch(() => null)
    if (!alive.current) return
    const next = res && (res as { portfolio?: Portfolio }).portfolio
    if (!next) {
      setFailed(true)
      setLoading(false)
      return
    }
    const entry = writeCache(uid, next)
    setPortfolio(next)
    setSavedAt(entry.savedAt)
    setLoading(false)
  }, [currentUser])

  useEffect(() => {
    alive.current = true
    void (async () => {
      const uid = await currentUser()
      if (!alive.current) return
      const cached = readCache(uid)
      if (cached) {
        // 命中缓存就直接显示，不发持仓 IPC —— 这正是「不要每次都重拉」。
        setPortfolio(cached.portfolio)
        setSavedAt(cached.savedAt)
        setLoading(false)
      } else {
        void fetchFresh()
      }
    })()
    return () => { alive.current = false }
  }, [fetchFresh, currentUser])

  return { portfolio, savedAt, loading, failed, refresh: fetchFresh }
}
