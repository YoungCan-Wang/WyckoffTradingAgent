/**
 * 账号菜单：锚在账号行**上方**（它本身贴着窗口底部）。
 *
 * 退出登录是破坏性的 —— 清掉会话与凭据，不会自动重登，所以要确认。
 */
import { useEffect, useRef } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Props {
  anchor: HTMLElement | null
  label: string
  initial: string
  signedIn: boolean
  onClose: () => void
  onSettings: () => void
  onSignOut: () => void
}

export function AccountMenu (
  { anchor, label, initial, signedIn, onClose, onSettings, onSignOut }: Props
) {
  const box = useRef<HTMLDivElement>(null)

  // 向上弹出，并夹住位置 —— 窗口很矮时不能把菜单顶出屏幕。
  useEffect(() => {
    if (!anchor || !box.current) return
    const r = anchor.getBoundingClientRect()
    const node = box.current
    node.style.left = `${Math.max(8, r.left + 6)}px`
    node.style.top = `${Math.max(8, r.top - node.offsetHeight - 6)}px`
    box.current.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [anchor])

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

  return (
    <div className="menu" role="menu" ref={box}>
      <div className="menu-hd">
        <span className="ava">{initial}</span>
        <span className="menu-em">{label}</span>
      </div>
      <button
        className="menu-i"
        role="menuitem"
        type="button"
        onClick={() => { onClose(); onSettings() }}
      >
        <i className="menu-g" data-lucide="settings" aria-hidden="true" />
        <span>{t('menu.settings')}</span>
      </button>
      {/* 没有会话时「退出登录」没有意义 */}
      {signedIn ? (
        <button
          className="menu-i"
          role="menuitem"
          type="button"
          onClick={() => { onClose(); onSignOut() }}
        >
          <i className="menu-g" data-lucide="log-out" aria-hidden="true" />
          <span>{t('menu.signout')}</span>
        </button>
      ) : null}
    </div>
  )
}
