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
  // 单调递增的请求序号，见 fetchFresh 里的说明。
  const requestSeq = useRef(0)

  /**
   * 当前账号。缓存按它分区，所以读缓存之前必须先知道「我是谁」——
   * 先读缓存再问账号的话，A 退出换 B 登录时 B 会看到 A 的持仓，而且因为命中
   * 缓存不发请求，这个错永远不会被纠正。
   *
   * 注意这只用于**读**缓存。写缓存必须用 portfolio 回传的 user_id ——
   * account 读的是磁盘上的登录态，portfolio 用的是会话里的工具上下文，两者
   * 可能不是同一个账号。用前者当 key 会把 A 的持仓存成 B 的。
   */
  const currentUser = useCallback(async (): Promise<string> => {
    const res = await collect('account').catch(() => null)
    return String((res as { user_id?: string } | null)?.user_id || '')
  }, [])

  const fetchFresh = useCallback(async () => {
    // 请求序号：只有最后一次发出的请求可以写 state。
    //
    // 账号切换时 onIdentityChange 会 setPortfolio(null) 再重新拉；此时上一个
    // 账号的请求可能还在路上，晚一步回来就把**旧账号的持仓渲染到新账号界面上**
    // —— 缓存层专门防的事，React state 这条路原来没防。
    // （只有 alive 标志不够：它只区分「组件还在吗」，不区分「这是第几次请求」。）
    const seq = ++requestSeq.current
    const stale = () => !alive.current || seq !== requestSeq.current

    setLoading(true)
    setFailed(false)
    const res = await collect('portfolio').catch(() => null)
    if (stale()) return
    const payload = res as { portfolio?: Portfolio; user_id?: string } | null
    const next = payload && payload.portfolio
    if (!next) {
      setFailed(true)
      setLoading(false)
      return
    }
    if (stale()) return
    // 用后端回传的账号，不是我们自己问来的那个 —— 这份数据实际属于谁只有它知道。
    const entry = writeCache(String(payload.user_id || ''), next)
    setPortfolio(next)
    setSavedAt(entry.savedAt)
    setLoading(false)
  }, [])

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

  /**
   * 登录态变了：把已经渲染出来的持仓从 state 里也清掉，然后重拉。
   *
   * 只清 localStorage 不够 —— 在持仓页上打开设置、退出登录、关掉设置，页面
   * 从没卸载过，React state 里还是上一个账号的仓位，一直显示到手动刷新或
   * 切页面为止。清缓存只挡住了「下次进页面」，挡不住「此刻正看着」。
   */
  useEffect(() => {
    const onIdentityChange = () => {
      setPortfolio(null)
      setSavedAt(null)
      void fetchFresh()
    }
    window.addEventListener('wyckoff:account-changed', onIdentityChange)
    return () => window.removeEventListener('wyckoff:account-changed', onIdentityChange)
  }, [fetchFresh])

  return { portfolio, savedAt, loading, failed, refresh: fetchFresh }
}
