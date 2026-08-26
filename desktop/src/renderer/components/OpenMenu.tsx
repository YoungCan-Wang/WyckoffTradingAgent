/**
 * 「打开」菜单：报告库 / K 线 / 浏览器 / 产物面板。
 *
 * 这些是 agent 产出的内容，不是导航目的地 —— 所以收在一个有标签的菜单里，
 * 而不是一排看不懂的字形按钮。
 */
import { useEffect, useRef } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

export interface MenuEntry {
  id: string
  icon: string
  labelKey: string
  shortcut: string
  /**
   * anchor 是触发菜单的那个按钮 —— 需要在它下方定位浮层的项（K 线输入框）
   * 要用它。命令式那侧引用不到 React 渲染的按钮。
   */
  onPick: (anchor: HTMLElement) => void
  hidden?: boolean
  /** 前面加一条分隔线。 */
  separated?: boolean
}

interface Props {
  anchor: HTMLElement | null
  entries: MenuEntry[]
  onClose: () => void
}

export function OpenMenu ({ anchor, entries, onClose }: Props) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => { window.WyckoffIcons?.render?.() })

  // 贴着触发按钮下方；窗口太窄时向左收，别把菜单推出屏幕。
  useEffect(() => {
    if (!anchor || !box.current) return
    const r = anchor.getBoundingClientRect()
    const node = box.current
    node.style.top = `${r.bottom + 6}px`
    node.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - node.offsetWidth - 8))}px`
  }, [anchor])

  // 点外面或 Esc 关掉；上下键在项间移动 —— 键盘用户不该被困在菜单里。
  useEffect(() => {
    if (!anchor) return
    const onDown = (e: MouseEvent) => {
      if (box.current?.contains(e.target as Node)) return
      if (anchor.contains(e.target as Node)) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); anchor.focus(); return }
      // 上下键在项间移动，Home/End 跳到首尾 —— 桌面菜单的标准行为。
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
      e.preventDefault()
      const items = [...(box.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || [])]
        .filter((n) => !n.hidden)
      if (!items.length) return
      if (e.key === 'Home') { items[0].focus(); return }
      if (e.key === 'End') { items[items.length - 1].focus(); return }
      const at = items.indexOf(document.activeElement as HTMLElement)
      const next = e.key === 'ArrowDown'
        ? (at + 1) % items.length
        : (at <= 0 ? items.length - 1 : at - 1)
      items[next].focus()
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [anchor, onClose])

  if (!anchor) return null
  const shown = entries.filter((e) => !e.hidden)

  return (
    <div className="menu" role="menu" ref={box}>
      {shown.map((e, i) => (
        <div key={e.id}>
          {e.separated && i > 0 ? <div className="menu-sep" /> : null}
          <button
            className="menu-i"
            role="menuitem"
            type="button"
            onClick={() => { onClose(); e.onPick(anchor) }}
          >
            <i className="menu-g" data-lucide={e.icon} aria-hidden="true" />
            <span>{t(e.labelKey)}</span>
            <span className="menu-k">{e.shortcut}</span>
          </button>
        </div>
      ))}
    </div>
  )
}
