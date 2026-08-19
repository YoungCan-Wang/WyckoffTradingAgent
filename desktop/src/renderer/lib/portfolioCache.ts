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
const KEY = 'wyckoff.portfolio.cache'

export interface CachedPortfolio {
  /** 写入缓存的本地时间戳 —— 「我什么时候拉的」。 */
  savedAt: number
  portfolio: Portfolio
}

export function readCache (): CachedPortfolio | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CachedPortfolio
    // 结构校验：旧版本或被外部改坏的数据宁可丢掉重拉，也不要渲染半个对象。
    if (!parsed || typeof parsed.savedAt !== 'number' || !parsed.portfolio) return null
    if (!Array.isArray(parsed.portfolio.positions)) return null
    return parsed
  } catch {
    return null
  }
}

export function writeCache (portfolio: Portfolio): CachedPortfolio {
  const entry: CachedPortfolio = { savedAt: Date.now(), portfolio }
  try {
    localStorage.setItem(KEY, JSON.stringify(entry))
  } catch {
    // 私密模式或配额满：内存里照常用，只是下次启动没有缓存。
  }
  return entry
}

export function clearCache (): void {
  try {
    localStorage.removeItem(KEY)
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
