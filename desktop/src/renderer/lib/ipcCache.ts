/**
 * 一次性 IPC 读取的**跨挂载**缓存。
 *
 * ## 为什么需要
 *
 * `useIpc` 原来每次挂载都重新 `collect()`。而页面是条件渲染的
 * (`PagePane.tsx`: `view === 'x' ? <Page/> : null`),切走就卸载、切回就重挂载,
 * 于是**每次进页面都重拉一遍**。
 *
 * 实测:跟踪页再进入 ~200ms 回来,归因页 1.6 秒还在转。两者都在重拉,
 * 只是归因慢到能被看见 —— 所以「跟踪页没问题」其实是错觉,它一样在重请求。
 *
 * 缓存存在**模块作用域**而不是组件 state:组件 state 随卸载一起消失,
 * 那正是归因页已经有了 `cache` 却依然每次转圈的原因。
 *
 * ## 为什么不设过期时间
 *
 * 这些数据(跟踪记录、归因报告)都是**已经发生的事实**,不会自己变。
 * 变的时候一定有人做了动作 —— 那条路径去 `invalidate()`,比到点重拉更准。
 * 需要新数据时页面上有刷新按钮。
 *
 * 账号切换会整体清空:别人的数据不该出现在你的界面上。这与
 * portfolioCache 的分区策略同理,只是这里的数据都是只读视图,
 * 直接全清比按账号分区更简单可靠。
 */
import type { PyEvent } from '../types'

/** key 是 `method + 序列化后的 params`。 */
const store = new Map<string, PyEvent | null>()

export function cacheKey (method: string, params?: Record<string, unknown>): string {
  return `${method}:${JSON.stringify(params ?? {})}`
}

export function readIpcCache (key: string): { hit: boolean; value: PyEvent | null } {
  // 用 has() 而不是「取出来看是不是 undefined」—— 成功但空结果的 null
  // 也是有效缓存,不该被当成未命中而反复重拉。
  return store.has(key) ? { hit: true, value: store.get(key) ?? null } : { hit: false, value: null }
}

export function writeIpcCache (key: string, value: PyEvent | null): void {
  store.set(key, value)
}

/** 丢掉某个方法的所有缓存(不管 params)。数据被改动之后调。 */
export function invalidateIpcCache (method: string): void {
  const prefix = `${method}:`
  for (const k of [...store.keys()]) {
    if (k.startsWith(prefix)) store.delete(k)
  }
}

/** 全清。账号切换时用 —— 那一刻拿不到「之前是谁」,逐个清不可靠。 */
export function clearIpcCache (): void {
  store.clear()
}
