/**
 * 侧边栏下方的会话列表。
 *
 * 父级持有列表状态、子行只管交互 —— 仿 SchedulesPage 的做法（它的注释说明了
 * 为什么行级 state 要上提：放在行里会被 reload 重建时清掉）。
 *
 * 没有虚拟滚动：项目里没这个依赖，既有做法是服务端限量（TrackingPage limit=200）
 * 加客户端筛。会话按 60 条取，够用很久。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { collect } from '../lib/ipc'
import { filterSessions, nextAfterRemoval, sortSessions, type Session } from '../lib/sessions'
import SessionRow from './SessionRow'

interface Props {
  activeId: string
  onSwitch: (id: string) => void
  /** 会话被删掉且没有下一个时，让上层开一个新的。 */
  onNeedNew: () => void
  reloadKey: number
  t: (key: string, params?: Record<string, string>) => string
}

export default function SessionList ({ activeId, onSwitch, onNeedNew, reloadKey, t }: Props) {
  const [rows, setRows] = useState<Session[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    const res = await collect('chat_sessions', { limit: 60 })
    setLoading(false)
    if (!res) return
    setRows(((res.sessions as Session[]) || []))
  }, [])

  // reloadKey 变化时重拉：上层在每轮对话结束后 +1，好让新会话和更新过的
  // 时间戳出现在列表里。
  useEffect(() => { void load() }, [load, reloadKey])

  useEffect(() => { window.WyckoffIcons?.render?.() }, [rows, query])

  // 本地筛 + 排序，不等往返 —— 重命名/置顶之后位置要立刻变。后端那份是权威。
  const visible = useMemo(() => sortSessions(filterSessions(rows, query)), [rows, query])

  const rename = async (id: string, title: string) => {
    // 乐观更新：等一次往返再改标题会让输入框刚提交完还显示旧值。
    setRows((prev) => prev.map((s) => (s.session_id === id ? { ...s, title } : s)))
    await collect('chat_rename', { session_id: id, title })
    void load()
  }

  const pin = async (id: string, pinned: boolean) => {
    setRows((prev) => prev.map((s) => (s.session_id === id ? { ...s, pinned: pinned ? 1 : 0 } : s)))
    await collect('chat_pin', { session_id: id, pinned })
    void load()
  }

  // 侧栏只提供归档，不提供删除 —— 删除是不可逆的，放在随手可点的悬停按钮上
  // 太危险。真要删得去设置页的已归档区，那里离手远、且必然先经过归档这一步。
  const archive = async (id: string) => {
    // 先算好落脚会话再归档 —— 归档完 rows 就没这一行了，算不出来。
    const next = nextAfterRemoval(rows, id, activeId)
    await collect('chat_archive', { session_id: id, archived: true })
    setRows((prev) => prev.filter((s) => s.session_id !== id))
    if (id === activeId) {
      if (next) onSwitch(next)
      else onNeedNew()
    }
    void load()
  }

  return (
    <div className="sess-wrap" data-testid="session-list">
      <div className="sess-head">
        <span className="nav-label">{t('session.groupLabel')}</span>
        {/* 搜索框只在会话多到需要找的时候才有意义 */}
        {rows.length > 6 ? (
          <input
            className="sess-search"
            type="search"
            value={query}
            placeholder={t('session.searchPlaceholder')}
            aria-label={t('session.searchLabel')}
            onChange={(e) => setQuery(e.target.value)}
          />
        ) : null}
      </div>

      <div className="sess-scroll">
        {loading ? <div className="sess-empty">{t('session.loading')}</div> : null}
        {!loading && visible.length === 0 ? (
          <div className="sess-empty">{query ? t('session.noMatch') : t('session.empty')}</div>
        ) : null}
        {visible.map((s) => (
          <SessionRow
            key={s.session_id}
            session={s}
            active={s.session_id === activeId}
            onOpen={onSwitch}
            onRename={rename}
            onPin={pin}
            onArchive={archive}
            t={t}
          />
        ))}
      </div>
    </div>
  )
}
