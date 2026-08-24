/**
 * 会话列表里的一行：点击切换，悬停出操作。
 *
 * 行内操作而不是右键菜单：这个项目里没有自绘上下文菜单的先例（OpenMenu 是
 * anchor 定位，不是坐标），照 ModelRow 的 .macts 模式做，少引一套范式。
 *
 * 重命名用行内输入而不是弹窗 —— 改标题是轻动作，弹个模态框太重。
 */
import { useEffect, useRef, useState } from 'react'
import { displayTitle, relativeTime, type Session } from '../lib/sessions'

interface Props {
  session: Session
  active: boolean
  onOpen: (id: string) => void
  onRename: (id: string, title: string) => void
  onPin: (id: string, pinned: boolean) => void
  onDelete: (id: string) => void
  t: (key: string, params?: Record<string, string>) => string
}

export default function SessionRow ({ session, active, onOpen, onRename, onPin, onDelete, t }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const input = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (editing) input.current?.select()
  }, [editing])

  const startRename = () => {
    setDraft(displayTitle(session, ''))
    setEditing(true)
  }

  const commit = () => {
    const next = draft.trim()
    setEditing(false)
    // 空标题会让条目看不出是什么；没改动就不必往返一趟。
    if (next && next !== displayTitle(session, '')) onRename(session.session_id, next)
  }

  if (editing) {
    return (
      <div className="sess-row editing">
        <input
          ref={input}
          className="sess-edit"
          value={draft}
          maxLength={60}
          aria-label={t('session.renameLabel')}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit()
            // Esc 放弃改动。没有它的话用户一旦开始编辑就只能提交或点走。
            if (e.key === 'Escape') setEditing(false)
          }}
        />
      </div>
    )
  }

  return (
    <div
      className={active ? 'sess-row on' : 'sess-row'}
      data-session={session.session_id}
      /* 与语言无关的选择器，供测试和 e2e 用 */
      data-active={active ? '1' : '0'}
    >
      <button
        type="button"
        className="sess-open"
        aria-current={active ? 'true' : undefined}
        onClick={() => onOpen(session.session_id)}
      >
        <span className="sess-title">
          {session.pinned ? <i className="sess-pin" data-lucide="pin" aria-hidden="true" /> : null}
          {displayTitle(session, t('session.untitled'))}
        </span>
        <span className="sess-meta">
          {relativeTime(session.ended_at)}
          {session.msg_count ? ` · ${t('session.msgCount', { n: String(session.msg_count) })}` : ''}
        </span>
      </button>

      <div className="sess-acts">
        <button
          type="button"
          className="sess-btn"
          title={session.pinned ? t('session.unpin') : t('session.pin')}
          aria-label={session.pinned ? t('session.unpin') : t('session.pin')}
          /* 行可点，所以按钮必须阻止冒泡，否则点操作会顺带切换会话 */
          onClick={(e) => { e.stopPropagation(); onPin(session.session_id, !session.pinned) }}
        >
          <i data-lucide={session.pinned ? 'pin-off' : 'pin'} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="sess-btn"
          title={t('session.rename')}
          aria-label={t('session.rename')}
          onClick={(e) => { e.stopPropagation(); startRename() }}
        >
          <i data-lucide="pencil" aria-hidden="true" />
        </button>
        <button
          type="button"
          className="sess-btn danger"
          title={t('session.delete')}
          aria-label={t('session.delete')}
          onClick={(e) => {
            e.stopPropagation()
            // 删除不可撤销，先确认。项目里四处删除都用原生 confirm。
            if (window.confirm(t('session.deleteConfirm'))) onDelete(session.session_id)
          }}
        >
          <i data-lucide="trash-2" aria-hidden="true" />
        </button>
      </div>
    </div>
  )
}
