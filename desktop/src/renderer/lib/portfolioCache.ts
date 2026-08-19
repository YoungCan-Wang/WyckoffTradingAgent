/**
 * 持仓快照的本地缓存。
 *
 * 持仓不像行情，不会自己变——只有你自己改，或者 CLI / 定时任务改。所以默认
 * 不自动重拉：进页面就显示上次的结果，要新的自己点刷新。
 *
 * 缓存故障不能拖垮页面：私密模式下 localStorage 会抛，配额满了写入也会抛。
 * 读写各自 try/catch，失败就当没有缓存。
 */
import type { Portfolio } from '../types'

// key 沿用现有约定（wyckoff.sidebar / wyckoff.lang）。
const PREFIX = 'wyckoff.portfolio.cache'

/**
 * 缓存必须按账号分区。
 *
 * 用固定 key 的后果不是「显示旧数据」这么轻：A 退出、B 登录，B 进持仓页会看到
 * A 的持仓，而且因为命中缓存**根本不请求后端**，所以不会被纠正。别人的仓位不该
 * 出现在你的界面上。
 *
 * 未登录用 __anon__ 单独一格：本地 SQLite 模式的持仓也不该跟任何云端账号混。
 */
function keyFor (userId: string): string {
  const id = String(userId || '').trim()
  return `${PREFIX}.${id || '__anon__'}`
}

export interface CachedPortfolio {
  /** 写入缓存的本地时间戳 —— 「我什么时候拉的」。 */
  savedAt: number
  portfolio: Portfolio
  /** 写入时的账号。与当前账号不符就整条作废 —— 双保险，key 已经分区了。 */
  userId?: string
}

export function readCache (userId: string): CachedPortfolio | null {
  try {
    const raw = localStorage.getItem(keyFor(userId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachedPortfolio
    // 结构校验：旧版本或被外部改坏的数据宁可丢掉重拉，也不要渲染半个对象。
    if (!parsed || typeof parsed.savedAt !== 'number' || !parsed.portfolio) return null
    if (!Array.isArray(parsed.portfolio.positions)) return null
    // 账号对不上就当没有缓存。key 分区之后这里理论上不会命中，但这是最后一道
    // 闸门 —— 宁可多拉一次，也不能把别人的持仓渲染出来。
    const expect = String(userId || '').trim() || '__anon__'
    if ((parsed.userId || '__anon__') !== expect) return null
    return parsed
  } catch {
    return null
  }
}

export function writeCache (userId: string, portfolio: Portfolio): CachedPortfolio {
  const entry: CachedPortfolio = {
    savedAt: Date.now(),
    portfolio,
    userId: String(userId || '').trim() || '__anon__'
  }
  try {
    localStorage.setItem(keyFor(userId), JSON.stringify(entry))
  } catch {
    // 私密模式或配额满：内存里照常用，只是下次启动没有缓存。
  }
  return entry
}

/** 清某个账号的缓存。 */
export function clearCache (userId: string): void {
  try {
    localStorage.removeItem(keyFor(userId))
  } catch {
    /* 同上，忽略 */
  }
}

/**
 * 清所有账号的持仓缓存。
 *
 * 用在登录态变化时：那一刻拿不到「之前是谁」，逐个清不可靠，而且残留的分区
 * 迟早会在换回该账号时命中过期数据。缓存本来就是可丢的，全清最省心。
 */
export function clearAllCaches (): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith(PREFIX)) doomed.push(k)
    }
    // 先收集再删：边遍历边删会跳过条目（索引会移位）。
    doomed.forEach((k) => localStorage.removeItem(k))
  } catch {
    /* 同上，忽略 */
  }
}

/**
 * 会改动持仓的工具。对话里跑了它们，缓存就是脏的。
 *
 * 这三个都在 cli/tools.py 里标了 requires_approval，但仍要在 tool_start 时
 * 就作废缓存：审批可能被「本次会话总是允许」放行，那条路径不产生审批事件。
 * 多清一次缓存的代价只是下次进页面重拉一遍，漏清一次的代价是你看着错的持仓
 * 做决定。
 */
const PORTFOLIO_WRITE_TOOLS = new Set(['update_portfolio', 'set_stop_loss', 'record_trade_fill'])

export function isPortfolioWriteTool (name: string): boolean {
  return PORTFOLIO_WRITE_TOOLS.has(String(name || ''))
}
