/**
 * 一次性 IPC 读取的加载状态。
 *
 * 关键是把「后端调用失败」和「成功但没数据」分开 —— collect() 两种情况都返回
 * null，混在一起会让「读取失败」显示成「暂无数据」，用户以为自己真的没有记录。
 *
 * 结果走模块级缓存(ipcCache),所以切走再切回来不会重拉。页面是条件渲染的,
 * 每次进出都会卸载/重挂载 —— 没有缓存时那就是每次都重新请求一遍。
 */
import { useCallback, useEffect, useState } from 'react'
import { collect } from './ipc'
import { cacheKey, readIpcCache, writeIpcCache } from './ipcCache'
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
  // params 逐次新建对象会让 effect 每次渲染都跑；用序列化后的值做依赖。
  const key = JSON.stringify(params ?? {})
  const ck = cacheKey(method, params)

  // 初始值直接读缓存 —— 命中时**首帧就是内容**,一帧的「读取中」都不闪。
  // 放在 useState 初始化里而不是 effect 里:effect 要等首次渲染之后才跑,
  // 那一帧仍然是 loading，视觉上就是「又转了一下」。
  const [state, setState] = useState<Omit<IpcState<T>, 'reload'>>(() => {
    const cached = readIpcCache(ck)
    if (cached.hit) return { data: cached.value as T | null, loading: false, failed: false }
    return { data: null, loading: true, failed: false }
  })

  // 变一下这个数就重新拉。比把 fetch 抽成 useCallback 再进依赖简单，也不会
  // 因为依赖数组写漏而悄悄不刷新。
  const [nonce, setNonce] = useState(0)
  const reload = useCallback(() => setNonce((n) => n + 1), [])

  useEffect(() => {
    let alive = true
    // nonce > 0 表示是显式 reload：那时绕过缓存,用户就是要新数据。
    if (nonce === 0) {
      const cached = readIpcCache(ck)
      if (cached.hit) {
        setState({ data: cached.value as T | null, loading: false, failed: false })
        return
      }
    }
    setState({ data: null, loading: true, failed: false })
    collect(method, params)
      .then((res) => {
        // 缓存写在 alive 判断**之前**：请求已经完成了,结果是有效的。
        // 放在后面的话,「切走→请求返回→切回」会丢掉这次结果又重拉一遍。
        if (res !== null) writeIpcCache(ck, res)
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
  }, [method, key, nonce, ck])

  return { ...state, reload }
}
