/**
 * 一次性 IPC 读取的加载状态。
 *
 * 关键是把「后端调用失败」和「成功但没数据」分开 —— collect() 两种情况都返回
 * null，混在一起会让「读取失败」显示成「暂无数据」，用户以为自己真的没有记录。
 */
import { useEffect, useState } from 'react'
import { collect } from './ipc'
import type { PyEvent } from '../types'

export interface IpcState<T> {
  data: T | null
  loading: boolean
  /** 调用失败（而非空结果）时为 true。 */
  failed: boolean
}

export function useIpc<T = PyEvent> (method: string, params?: Record<string, unknown>): IpcState<T> {
  const [state, setState] = useState<IpcState<T>>({ data: null, loading: true, failed: false })
  // params 逐次新建对象会让 effect 每次渲染都跑；用序列化后的值做依赖。
  const key = JSON.stringify(params ?? {})

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
  }, [method, key])

  return state
}
