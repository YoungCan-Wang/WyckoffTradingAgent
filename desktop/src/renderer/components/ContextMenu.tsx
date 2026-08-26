/**
 * 按坐标定位的右键菜单。
 *
 * ## 为什么不复用 OpenMenu
 *
 * OpenMenu 贴着一个**元素**定位（`anchor.getBoundingClientRect()`），右键菜单
 * 贴的是**鼠标位置**。两者的定位输入不同类型，硬塞进一个组件会得到一个
 * anchor 和 point 互斥、一半参数永远是 undefined 的接口。共用的部分是
 * `.menu` 那套 CSS 和「点外面关掉 / Esc 关掉 / 上下键移动」的行为，
 * 这些在下面重新实现一遍也就二十行，比拧一个双形态组件划算。
 *
 * ## 翻转而不是钳制
 *
 * 菜单在右下方展开。贴着窗口右边或底边右键时，钳到边上会盖住鼠标底下那一行，
 * 用户看不见自己点的是谁；所以改成朝反方向翻。
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export interface ContextItem {
  id: string
  icon: string
  label: string
  onPick: () => void
  /** 前面加一条分隔线。 */
  separated?: boolean
  /** 危险动作用红色（复用 .menu-i.danger）。 */
  danger?: boolean
}

interface Props {
  /** 视口坐标；null 表示菜单关闭。 */
  at: { x: number; y: number } | null
  items: ContextItem[]
  onClose: () => void
}

export default function ContextMenu ({ at, items, onClose }: Props) {
  const box = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)

  useEffect(() => { window.WyckoffIcons?.render?.() })

  // useLayoutEffect 而不是 useEffect：要在浏览器绘制前定好位置，否则菜单会先
  // 在左上角闪一下再跳到鼠标那里。
  useLayoutEffect(() => {
    if (!at || !box.current) { setPos(null); return }
    const node = box.current
    const w = node.offsetWidth
    const h = node.offsetHeight
    const pad = 8
    // 右边放不下就朝左开，下边放不下就朝上开 —— 翻转，不是钳制。
    const left = at.x + w + pad > window.innerWidth ? Math.max(pad, at.x - w) : at.x
    const top = at.y + h + pad > window.innerHeight ? Math.max(pad, at.y - h) : at.y
    setPos({ left, top })
  }, [at])

  // 打开后把焦点移到第一项，键盘用户不用先 Tab 进去。
  //
  // 依赖必须是 pos 而不是 at：首帧菜单是 visibility:hidden（还没量出尺寸,
  // 不能先在左上角闪一下）,而 visibility:hidden 的元素**不可聚焦** ——
  // 挂在 at 上的话 focus() 会静默失败,之后 pos 算出来了也不会再跑一次,
  // 结果菜单开着但上下键无效。
  useEffect(() => {
    if (!at || !pos) return
    box.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
  }, [at, pos])

  useEffect(() => {
    if (!at) return
    const onDown = (e: MouseEvent) => {
      if (box.current?.contains(e.target as Node)) return
      onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return }
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return
      e.preventDefault()
      const nodes = [...(box.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || [])]
      if (!nodes.length) return
      if (e.key === 'Home') { nodes[0].focus(); return }
      if (e.key === 'End') { nodes[nodes.length - 1].focus(); return }
      const cur = nodes.indexOf(document.activeElement as HTMLElement)
      const next = e.key === 'ArrowDown'
        ? (cur + 1) % nodes.length
        : (cur <= 0 ? nodes.length - 1 : cur - 1)
      nodes[next].focus()
    }
    // 滚动或改窗口大小时关掉：菜单是绝对定位的，页面一动它就指错行了。
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', onClose)
    window.addEventListener('scroll', onClose, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onClose)
      window.removeEventListener('scroll', onClose, true)
    }
  }, [at, onClose])

  if (!at) return null

  return (
    <div
      className="menu"
      role="menu"
      ref={box}
      data-context-menu="1"
      /* 首帧还没量出尺寸：先渲染但不可见，避免在左上角闪一下。 */
      style={pos
        ? { left: `${pos.left}px`, top: `${pos.top}px` }
        : { left: 0, top: 0, visibility: 'hidden' }}
    >
      {items.map((it, i) => (
        <div key={it.id}>
          {it.separated && i > 0 ? <div className="menu-sep" /> : null}
          <button
            className={it.danger ? 'menu-i danger' : 'menu-i'}
            role="menuitem"
            type="button"
            onClick={() => { onClose(); it.onPick() }}
          >
            <i className="menu-g" data-lucide={it.icon} aria-hidden="true" />
            <span>{it.label}</span>
          </button>
        </div>
      ))}
    </div>
  )
}
