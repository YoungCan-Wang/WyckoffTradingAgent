/**
 * 「会话」页：管理已归档的会话。
 *
 * 为什么删除在这里、而不在侧栏：删除会连着对话记录一起没，不可逆。放在侧栏
 * 那种悬停就能点到的位置太容易误触。这里是二级路径，而且要删必然先经过归档
 * 那一步 —— 相当于天然的两段确认。
 *
 * 自己拉一次列表（archived: true），不复用侧栏那份：那份刻意只有未归档的。
 */
import { useCallback, useEffect, useState } from 'react'
import { collect } from '../lib/ipc'
import { displayTitle, relativeTime, sortSessions, type Session } from '../lib/sessions'
import { SecHead } from './Rows'

const t = (key: string, params?: Record<string, string>) => window.WyckoffI18n.t(key, params)

/** busy 的哨兵值：表示「正在清空」，不是某一行在忙。会话 id 不会长这样。 */
const ALL = '*all*'

interface Props {
  /** 恢复或删除之后要让侧栏重拉 —— 那边的列表变了。 */
  onChanged: () => void
  onMessage: (text: string, isError?: boolean) => void
}

export function ArchivedChatsPanel ({ onChanged, onMessage }: Props) {
  const [rows, setRows] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  /** 正在操作的会话 id —— 按钮要禁用，避免连点发两次删除。
      清空时用 ALL 这个哨兵：那一下要把每一行的按钮都锁住。 */
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    const res = await collect('chat_sessions', { limit: 200, archived: true })
    setLoading(false)
    if (!res) { setFailed(true); return }
    setFailed(false)
    setRows((res.sessions as Session[]) || [])
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => { window.WyckoffIcons?.render?.() }, [rows])

  const restore = async (id: string) => {
    setBusy(id)
    await collect('chat_archive', { session_id: id, archived: false })
    setRows((prev) => prev.filter((s) => s.session_id !== id))
    setBusy('')
    onChanged()
    onMessage(t('session.restoreDone'))
  }

  const remove = async (id: string) => {
    if (!window.confirm(t('session.deleteConfirm'))) return
    setBusy(id)
    await collect('chat_delete', { session_id: id })
    setRows((prev) => prev.filter((s) => s.session_id !== id))
    setBusy('')
    // 删掉的可能正好是当前会话（先归档再删）—— 让上层重新对齐侧栏和活动会话。
    onChanged()
  }

  // 全部删除。一次 IPC 让后端按 archived=1 删完，不在这里循环单删 ——
  // 循环中间失败会留下删一半的状态，而用户以为这是个原子操作。
  const removeAll = async () => {
    const n = rows.length
    if (!n || !window.confirm(t('session.deleteAllConfirm', { n: String(n) }))) return
    setBusy(ALL)
    const res = await collect('chat_delete_archived')
    setBusy('')
    if (!res) { onMessage(t('daemon.readSettingsFailed'), true); return }
    // 用后端报的个数，不用前端以为的 —— 两者不一致时（并发改动）说实话。
    const deleted = Number(res.deleted ?? 0)
    setRows([])
    onChanged()
    onMessage(t('session.deleteAllDone', { n: String(deleted) }))
  }

  if (loading) return <p className="empty">{t('common.loading')}</p>
  if (failed) return <p className="empty">{t('daemon.readSettingsFailed')}</p>

  const visible = sortSessions(rows)

  return (
    <>
      <SecHead k="session.archivedTitle" />
      <p className="dlg-sub">{t('session.archivedHint')}</p>

      {visible.length === 0 ? (
        <p className="empty">{t('session.archivedEmpty')}</p>
      ) : (
        <>
          {/* 计数和「全部删除」放同一行：这个按钮的作用域就是这个计数。
              分开放会让人不确定它删的是哪些。 */}
          <div className="arch-bar">
            <span className="dlg-sub arch-bar-n">
              {t('session.archivedCount', { n: String(visible.length) })}
            </span>
            <button
              type="button"
              className="mbtn danger"
              disabled={Boolean(busy)}
              data-testid="delete-all-archived"
              onClick={() => void removeAll()}
            >
              {t('session.deleteAll')}
            </button>
          </div>
          {/* 复用模型列表那套行样式（.mrow/.minfo/.macts）：结构完全一样 ——
              左边标题加副行、右边一组按钮。另造一套 class 只会多出重复的 CSS。 */}
          <div data-testid="archived-list">
            {visible.map((s) => (
              <div className="mrow" key={s.session_id} data-session={s.session_id}>
                <div className="minfo">
                  <div className="mtitle"><span className="mid">{displayTitle(s, t('session.untitled'))}</span></div>
                  <div className="msub">
                    {relativeTime(s.ended_at)}
                    {s.msg_count ? ` · ${t('session.msgCount', { n: String(s.msg_count) })}` : ''}
                  </div>
                </div>
                <div className="macts">
                  {/* 清空进行中要锁住每一行，不只是「自己那一行在忙」的行 */}
                  <button
                    type="button"
                    className="mbtn"
                    disabled={busy === s.session_id || busy === ALL}
                    onClick={() => void restore(s.session_id)}
                  >
                    {t('session.unarchive')}
                  </button>
                  <button
                    type="button"
                    className="mbtn danger"
                    disabled={busy === s.session_id || busy === ALL}
                    onClick={() => void remove(s.session_id)}
                  >
                    {t('session.delete')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}
