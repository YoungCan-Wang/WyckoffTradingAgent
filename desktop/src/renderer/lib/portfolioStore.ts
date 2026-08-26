/**
 * 持仓的**单一数据源**。
 *
 * ## 为什么要有这一层
 *
 * 之前首页和持仓页各自 `collect('portfolio')`,互不相干:
 *
 * - `Welcome.tsx` 自己拉一次,deps 是 `[]`。而 ChatView 用 `hidden` 常驻挂载,
 *   于是那次请求**开机时跑一次就永不再跑** —— 登录前就跑完了。所以你在持仓页
 *   明明拉到了数据,首页的「持仓」还是 0,并且永远是 0。
 * - 登录后没有任何一条路径去拉持仓。
 * - 切走再切回持仓页,虽然缓存命中了,但读缓存前要先 await 一次 `account` IPC,
 *   那段往返里 `loading` 是 true —— 你看到的「又在加载」其实是账号请求,不是持仓。
 *
 * 这三件事是同一个根因:没有共享状态,每个组件维护自己的一份。
 *
 * ## 形态:store 持有数据,UI 订阅
 *
 * 这正是你说的 ViewModel 那套。React 的内置原语是 `useSyncExternalStore`,
 * 不需要引入 Redux / Zustand:
 *
 * - store 是模块级单例,持有唯一一份持仓
 * - 组件通过 `usePortfolio()` 订阅,数据变了自动重渲染
 * - 谁都可以 `refresh()` / `invalidate()`,所有订阅者一起更新
 *
 * 与 Android 的差别只在写法:那边 `LiveData.observe`,这边 `useSyncExternalStore`。
 * 语义一致 —— UI 不再自己取数据,只声明「我要看这份数据」。
 *
 * ## 关键约束(沿用旧实现里已经踩过的坑)
 *
 * 缓存按账号分区,读缓存前必须知道「我是谁」;写缓存必须用**后端回传的**
 * user_id,不是我们问来的那个。理由见 portfolioCache.ts 的注释。
 */
import { collect } from './ipc'
import { readCache, readSoleCache, writeCache } from './portfolioCache'
import type { Portfolio } from '../types'

export interface PortfolioSnapshot {
  portfolio: Portfolio | null
  /** 缓存写入时间;null 表示这份数据是刚拉的。 */
  savedAt: number | null
  loading: boolean
  failed: boolean
  /**
   * 后端给的失败原因,空串表示没有具体原因(比如整个调用没回来)。
   *
   * 单独一个字段而不是塞进 failed:失败时页面要能说清「云端连不上」还是
   * 别的什么,而不是一句通用的「读取失败」—— 后者看不出该刷新还是该查网络。
   */
  error: string
}

/**
 * 当前快照。**必须是稳定引用** —— `useSyncExternalStore` 每次渲染都会调
 * `getSnapshot()` 并用 `Object.is` 比对,每次返回新对象会无限重渲染。
 * 所以只在真的变化时整体替换。
 */
let snapshot: PortfolioSnapshot = {
  portfolio: null,
  savedAt: null,
  // 初始是 false 而不是 true:还没人要过数据,不该显示「加载中」。
  // 真正开始拉的时候才置 true。
  loading: false,
  failed: false,
  error: ''
}

const listeners = new Set<() => void>()

/**
 * 已知的当前账号。
 *
 * 记在内存里,是为了消掉「切回持仓页又转一次圈」:第一次要 await account IPC,
 * 之后直接命中,`getSnapshot()` 同步就能给出缓存里的持仓,一帧都不闪。
 *
 * `null` = 还不知道自己是谁;`''` = 确定是未登录。两者必须分开 —— 见 expectedUser。
 */
let knownUser: string | null = null

/**
 * 这一刻**应该**看到谁的持仓。`null` 表示不知道。
 *
 * 为什么不能只用 knownUser：`invalidate()`（换账号时调）会把它置成 null 来
 * 强制重拉,那正好**关掉了账号核对** —— 而换账号恰恰是最需要核对的时刻。
 * 后端如果此刻锁忙、返回上一个账号的持仓,就会被直接渲染出来。
 *
 * 所以从 `account-changed` 事件里把新 userId 接过来单独记着。它跟
 * knownUser 的区别是：
 *
 * - knownUser 是「我上次拿到的数据属于谁」，会被 refresh 覆盖
 * - expectedUser 是「我现在应该是谁」，只在账号真的变化时更新
 *
 * 空串是**有意义的值**（退出登录后应该看到「无账号」的持仓，也就是本地那份）。
 * 用 null 表示未知，才能把「未登录」和「还不知道」区分开 —— 原来的
 * `knownUser !== ''` 判断把退出登录的场景整个漏掉了。
 */
let expectedUser: string | null = null

/** 单调递增请求号:只有最后一次发出的请求可以写状态。见 fetchFresh。 */
let requestSeq = 0

/** 是否已经有人触发过加载 —— 避免多个组件同时挂载时重复拉。 */
let started = false

/**
 * 账号不匹配时是否已经自动重试过。
 *
 * 只重试一次:后端的身份同步在长对话期间可能连续几次都返回旧账号,不设上限
 * 就是个无限请求循环 —— 比显示一次错数据更糟。
 */
let mismatchRetried = false

const t = (key: string) => window.WyckoffI18n.t(key)

function emit (next: Partial<PortfolioSnapshot>): void {
  snapshot = { ...snapshot, ...next }
  for (const fn of listeners) fn()
}

export function subscribe (fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

export function getSnapshot (): PortfolioSnapshot {
  return snapshot
}

async function currentUser (): Promise<string> {
  if (knownUser !== null) return knownUser
  const res = await collect('account').catch(() => null)
  knownUser = String((res as { user_id?: string } | null)?.user_id || '')
  return knownUser
}

/**
 * 强制重拉。手动刷新、写入之后、账号变化都走这里。
 */
export async function refresh (): Promise<void> {
  // 请求号:账号切换时上一个账号的请求可能还在路上,晚一步回来就会把旧账号的
  // 持仓渲染到新账号界面上。只判断「组件还在吗」不够,要判断「这是第几次请求」。
  const seq = ++requestSeq
  const stale = () => seq !== requestSeq

  emit({ loading: true, failed: false, error: '' })
  const res = await collect('portfolio').catch(() => null)
  if (stale()) return

  const payload = res as { portfolio?: Portfolio & { error?: string }; user_id?: string } | null
  const next = payload && payload.portfolio
  if (!next) {
    emit({ loading: false, failed: true, error: '' })
    return
  }
  // 后端**成功返回了一个装着错误的对象**：`{portfolio: {error: "..."}}`。
  //
  // 这个分支是实测踩出来的：云端 TLS 握手超时时,portfolio 工具返回
  // `{"portfolio": {"error": "handshake timed out"}}`。它是 truthy,所以原来
  // 直接当成一份合法持仓存进缓存并渲染 —— 图表拿不到 positions,画出
  // 「暂无持仓数据」。于是**网络抖一下,你的持仓看起来就清零了**,而且页面
  // 不提示任何异常,你不知道该点刷新。
  //
  // 失败要说是失败。也不写缓存 —— 把错误对象缓存下来,下次进页面连请求都不发。
  const failure = String(next.error || '')
  if (failure) {
    emit({ loading: false, failed: true, error: failure })
    return
  }
  // 用后端回传的账号,不是我们自己问来的那个 —— 这份数据属于谁只有它知道。
  const owner = String(payload.user_id || '')

  // **核对这份数据是不是当前账号的。**
  //
  // 后端的身份同步在对话进行中会跳过对齐（拿不到锁），此时 portfolio 返回的
  // 可能是**上一个账号**的持仓 —— 而它带回来的 user_id 也如实是上一个账号。
  // 不核对就直接渲染:缓存分区是对的（不会张冠李戴），但**界面上摆的是别人的
  // 仓位**,而且看不出异常。
  //
  // 基准优先用 expectedUser（`account-changed` 事件里的新账号），退回 knownUser。
  // 我上一版只看 knownUser，有两个洞，都是复审指出来的：
  //
  //   1. invalidate() 把 knownUser 置 null 来强制重拉，于是**换账号那一刻**
  //      核对被整个跳过 —— 而那正是最需要它的时候；
  //   2. `knownUser !== ''` 把退出登录也漏了：预期账号是空串，
  //      后端却返回 alice 的持仓，一样被放行。
  //
  // 现在用 `!= null` 判空，空串照样参与比较。缓存仍按真实 owner 写入 ——
  // 那份数据本身有效，只是不属于现在这个人。
  const expected = expectedUser !== null ? expectedUser : knownUser
  if (expected !== null && owner !== expected) {
    writeCache(owner, next)
    // 只自动重试一次。递归 refresh 不设上限的话，后端如果一直返回旧账号
    // （比如那一轮对话很长），这里就是个无限请求循环 —— 比显示错数据更糟。
    if (!mismatchRetried) {
      mismatchRetried = true
      void refresh()
      return
    }
    mismatchRetried = false
    emit({ loading: false, failed: true, error: t('portfolio.accountMismatch') })
    return
  }
  mismatchRetried = false

  knownUser = owner
  // 核对通过了,把预期也对齐到实到的账号。不清的话 expectedUser 会一直钉在
  // 换账号那一刻的值上,而 knownUser 之后可能正常演进(比如首次登录时
  // 从空串变成真实 uid),两者就会开始互相矛盾。
  expectedUser = owner
  const entry = writeCache(owner, next)
  emit({ portfolio: next, savedAt: entry.savedAt, loading: false, failed: false, error: '' })
}

/**
 * 确保数据已加载。有缓存就用缓存,不发请求 —— 持仓不会自己变。
 *
 * 幂等:多个组件同时挂载只会触发一次。
 */
export async function ensureLoaded (): Promise<void> {
  if (started) return
  started = true

  // 先同步试一把:只有一个缓存分区时可以直接用,不必等 account IPC。
  // 打包后 Python 冷启动十几秒,那期间首页只能显示占位符 —— 而磁盘上明明
  // 就有上次的持仓。这一步把等待从「十几秒」压到「零」。
  const sole = readSoleCache()
  if (sole) {
    emit({ portfolio: sole.portfolio, savedAt: sole.savedAt, loading: false, failed: false, error: '' })
    // 拿到账号后核对一次:分区对不上说明这份不是当前用户的,重拉。
    void (async () => {
      const uid = await currentUser()
      const mine = readCache(uid)
      if (!mine) await refresh()
    })()
    return
  }

  emit({ loading: true })
  const uid = await currentUser()
  const cached = readCache(uid)
  if (cached) {
    emit({ portfolio: cached.portfolio, savedAt: cached.savedAt, loading: false, failed: false, error: '' })
    return
  }
  await refresh()
}

/**
 * 账号变了:清掉内存里的持仓和账号,重新拉。
 *
 * 只清 localStorage 不够 —— 正在看着的界面上,React 里还是上一个账号的仓位。
 */
export function invalidate (nextUser?: string | null): void {
  knownUser = null
  // 记下「现在应该是谁」。**必须接这个参数** —— 只清 knownUser 的话，
  // 下一次 refresh 就没有基准可比，后端锁忙返回旧账号时会被直接渲染出来。
  //
  // `undefined`（调用方没传）→ null，表示不知道，退回旧行为；
  // 空串是有意义的值（退出登录），照样存进去参与比较。
  expectedUser = nextUser === undefined ? null : nextUser
  mismatchRetried = false
  started = true
  emit({ portfolio: null, savedAt: null, loading: true, failed: false, error: '' })
  void refresh()
}

/** 供测试重置。生产代码不该调。 */
export function __reset (): void {
  snapshot = { portfolio: null, savedAt: null, loading: false, failed: false, error: '' }
  listeners.clear()
  knownUser = null
  expectedUser = null
  requestSeq = 0
  started = false
  mismatchRetried = false
}
