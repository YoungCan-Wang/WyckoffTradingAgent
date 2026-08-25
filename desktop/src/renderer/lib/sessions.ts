/**
 * 会话列表的纯逻辑：排序、筛选、显示标题。
 *
 * 抽成纯函数是为了能真测 —— 这个项目的组件测试都是读源码字符串断言
 * （没有 jsdom），只有纯函数有真实行为覆盖（见 tracking.ts / portfolioCache.ts）。
 *
 * 排序和搜索后端也做了。前端再做一遍是因为：重命名/置顶之后要立刻看到位置变化，
 * 等一次往返再重排会让列表跳一下。后端那份是权威，这份是即时反馈。
 */

export interface Session {
  session_id: string
  title: string
  pinned: number
  /** 1 = 已归档，从侧栏收起，只在设置页可见。 */
  archived?: number
  msg_count: number
  started_at: string
  ended_at: string
  model?: string
  first_user_msg?: string
}

/** 列表里显示的标题。空标题会让条目看不出是什么，给个明确的占位。 */
export function displayTitle (s: Session, fallback = '未命名对话'): string {
  const title = (s.title || '').trim()
  if (title) return title
  // 后端已经清洗过（去掉注入的时间戳），这里只是兜底：老数据或异常行。
  const first = (s.first_user_msg || '').split('\n')[0].trim()
  return first || fallback
}

/**
 * 置顶优先，其余按最近活动倒序。
 *
 * 用 ended_at（最后一条消息的时间）而不是 started_at：用户找的是「刚才聊的那个」，
 * 而一个上周开的会话如果今天又聊了，它应该在最前面。
 */
export function sortSessions (rows: Session[]): Session[] {
  return [...rows].sort((a, b) => {
    const pin = (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)
    if (pin !== 0) return pin
    return (b.ended_at || '').localeCompare(a.ended_at || '')
  })
}

/**
 * 按关键词筛。匹配标题和首条提问两处 —— 用户可能记得自己起的名字，
 * 也可能只记得当时问了什么。
 *
 * 大小写不敏感（英文股票代码、ticker 常混着大小写写）。
 */
export function filterSessions (rows: Session[], query: string): Session[] {
  const q = query.trim().toLowerCase()
  if (!q) return rows
  return rows.filter((s) => {
    const haystack = `${s.title || ''} ${s.first_user_msg || ''}`.toLowerCase()
    return haystack.includes(q)
  })
}

/** 相对时间。列表里空间有限，「3天前」比完整时间戳好扫。 */
export function relativeTime (iso: string, now = Date.now()): string {
  if (!iso) return ''
  // SQLite 的 datetime('now') 是 UTC 但不带时区标记，直接 new Date() 会被当成
  // 本地时间 —— 那会让刚发生的事显示成「8小时前」。补上 Z。
  const stamp = iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z'
  const then = new Date(stamp).getTime()
  if (!Number.isFinite(then)) return ''
  const mins = Math.floor((now - then) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins}分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}天前`
  const months = Math.floor(days / 30)
  return months < 12 ? `${months}个月前` : `${Math.floor(months / 12)}年前`
}

/**
 * 一个会话离开列表后该切到哪个。删除和归档共用 —— 对列表来说两者都是「这一行
 * 不在了」，落脚逻辑完全一样，没必要写两份。
 *
 * 走的不是当前会话时不切（用户只是在整理列表，不该被拽走）。走的是当前会话时
 * 切到列表里的下一个，没有了就返回空串让调用方开新会话。
 */
export function nextAfterRemoval (rows: Session[], removedId: string, activeId: string): string {
  if (removedId !== activeId) return activeId
  const remaining = sortSessions(rows).filter((s) => s.session_id !== removedId)
  return remaining[0]?.session_id || ''
}
