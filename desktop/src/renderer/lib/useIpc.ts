/**
 * 一次性 IPC 读取的加载状态。
 *
 * 关键是把「后端调用失败」和「成功但没数据」分开 —— collect() 两种情况都返回
 * null，混在一起会让「读取失败」显示成「暂无数据」，用户以为自己真的没有记录。
 */
import { useCallback, useEffect, useState } from 'react'
import { collect } from './ipc'
import type { PyEvent } from '../types'

export interface IpcState<T> {
  data: T | null
  loading: boolean
  /** 调用失败（而非空结果）时为 true。 */
  failed: boolean
  /** 重新拉一次。审批、重跑这类「做完要看新状态」的动作需要它。 */
  reload: () => void
}

export function useIpc<T = PyEvent> (method: string, params?: Record<string, unknown>): IpcState<T> {
  const [state, setState] = useState<Omit<IpcState<T>, 'reload'>>({ data: null, loading: true, failed: false })
  // params 逐次新建对象会让 effect 每次渲染都跑；用序列化后的值做依赖。
  const key = JSON.stringify(params ?? {})
  // 变一下这个数就重新拉。比把 fetch 抽成 useCallback 再进依赖简单，也不会
  // 因为依赖数组写漏而悄悄不刷新。
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    setState({ data: null, loading: true, failed: false })
    collect(method, params)
      .then((res) => {
        if (!alive) return
        setState({ data: res as T | null, loading: false, failed: res === null })
      })
      .catch(() => {
        if (!alive) return
        setState({ data: null, loading: false, failed: true })
      })
    // 组件已卸载就别再 setState —— 页面切换比请求返回快时会警告。
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [method, key, nonce])

  return { ...state, reload }
}
