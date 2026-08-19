/**
 * IPC 调用封装。桥是「发起调用 → 监听同 id 的事件流 → end 收尾」，
 * 不是普通的 request/response，所以每个调用都要自己管监听器的生命周期。
 */
import type { PyEvent } from '../types'

/**
 * 发一次调用，等这一轮结束，返回 result 事件（没有则 null）。
 *
 * 与 vanilla 版 collect() 行为一致：失败或无结果都是 null。
 * 注意必须在 end 时解绑 —— 否则每次调用都会漏一个监听器，
 * 长会话下同一个事件会被处理几十次。
 */
export function collect (method: string, params?: Record<string, unknown>): Promise<PyEvent | null> {
  return window.wyckoff.call(method, params).then((res) => {
    if (!res.ok || !res.id) return null
    return new Promise<PyEvent | null>((resolve) => {
      let payload: PyEvent | null = null
      const off = window.wyckoff.onEvent((event) => {
        if (event.id !== res.id) return
        if (event.type === 'result') payload = event
        if (event.type === 'end') {
          off()
          resolve(payload)
        }
      })
    })
  })
}

/**
 * 同上，但失败时抛出后端给的错误消息。
 *
 * 写操作必须用这个：collect() 把一切失败压成 null，于是「股数必须大于 0」
 * 和「网络断了」在界面上长得一样。后端的校验消息本身就是最好的提示文案，
 * 不该被吞掉再由前端编一句含糊的话。
 */
export function callWithError (
  method: string,
  params?: Record<string, unknown>
): Promise<PyEvent | null> {
  return window.wyckoff.call(method, params).then((res) => {
    if (!res.ok) throw new Error(String(res.error || `调用 ${method} 失败`))
    if (!res.id) throw new Error(`调用 ${method} 没有返回流 id`)
    return new Promise<PyEvent | null>((resolve, reject) => {
      let payload: PyEvent | null = null
      let failure: Error | null = null
      const off = window.wyckoff.onEvent((event) => {
        if (event.id !== res.id) return
        if (event.type === 'result') payload = event
        if (event.type === 'error') {
          failure = new Error(String(event.message || event.error || `${method} 失败`))
        }
        if (event.type === 'end') {
          off()
          if (failure) reject(failure)
          else resolve(payload)
        }
      })
    })
  })
}
