/**
 * 会话列表里的一行：点击切换，悬停出操作，右键出菜单。
 *
 * 两个入口都留着,不是重复:
 * - 悬停按钮是**可发现性** —— 新用户不会去猜某处能右键。
 * - 右键是**效率** —— 侧栏只有 224px 宽,三个 22px 的图标挤在标题右边,
 *   在窄侧栏里点中它们要瞄。右键给的是一个有文字标签的大目标。
 *
 * 重命名用行内输入而不是弹窗 —— 改标题是轻动作，弹个模态框太重。
 */
import { useEffect, useRef, useState } from 'react'
import { displayTitle, relativeTime, type Session } from '../lib/sessions'
import ContextMenu, { type ContextItem } from './ContextMenu'

interface Props {
  session: Session
  active: boolean
  onOpen: (id: string) => void
  onRename: (id: string, title: string) => void
  onPin: (id: string, pinned: boolean) => void
  onArchive: (id: string) => void
  t: (key: string, params?: Record<string, string>) => string
}

export default function SessionRow ({ session, active, onOpen, onRename, onPin, onArchive, t }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [menuAt, setMenuAt] = useState<{ x: number; y: number } | null>(null)
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

  // 右键菜单项。和悬停按钮走同一批回调 —— 两个入口不该有行为差异。
  const menuItems: ContextItem[] = [
    {
      id: 'rename',
      icon: 'pencil',
      label: t('session.rename'),
      onPick: startRename
    },
    {
      id: 'pin',
      icon: session.pinned ? 'pin-off' : 'pin',
      label: session.pinned ? t('session.unpin') : t('session.pin'),
      onPick: () => onPin(session.session_id, !session.pinned)
    },
    {
      id: 'archive',
      icon: 'archive',
      label: t('session.archive'),
      separated: true,
      onPick: () => onArchive(session.session_id)
    }
  ]

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
      onContextMenu={(e) => {
        // preventDefault 挡掉浏览器默认菜单。主进程那个原生菜单不会和它撞 ——
        // main.js 的 attachContextMenu 只在有选区/链接/可编辑区时才 popup,
        // 会话行三样都没有,它会提前 return。
        e.preventDefault()
        setMenuAt({ x: e.clientX, y: e.clientY })
      }}
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
        {/* 归档不确认：它可逆，去设置页一键就能恢复。真正不可逆的删除只在
            设置页的已归档区出现，那里才弹 confirm。 */}
        <button
          type="button"
          className="sess-btn"
          title={t('session.archive')}
          aria-label={t('session.archive')}
          onClick={(e) => { e.stopPropagation(); onArchive(session.session_id) }}
        >
          <i data-lucide="archive" aria-hidden="true" />
        </button>
      </div>

      <ContextMenu at={menuAt} items={menuItems} onClose={() => setMenuAt(null)} />
    </div>
  )
}
