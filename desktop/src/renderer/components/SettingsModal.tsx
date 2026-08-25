/**
 * 设置弹窗：左侧三个栏目 + 右侧面板。
 *
 * 打开时把背景区域设为 inert —— 否则背景里的按钮仍能被 Tab 到，键盘用户会
 * 「掉出」这个模态框。弹窗本身也在 .win 内，不能直接把整个 .win 设为 inert。
 */
import { useEffect, useRef } from 'react'
import { SettingsPanel } from './SettingsPanel'

const t = (key: string) => window.WyckoffI18n.t(key)

const SECTIONS: Array<[string, string]> = [
  ['general', 'settings.groupGeneral'],
  ['agent', 'settings.groupAgent'],
  ['chats', 'settings.groupChats'],
  ['account', 'settings.groupAccount']
]

interface Props {
  open: boolean
  section: string
  /** 可选：把某个分栏标题滚到顶部（i18n key）。 */
  anchor?: string
  onSection: (sec: string) => void
  onClose: () => void
  onMessage: (text: string, isError?: boolean) => void
  onSignOut: () => void
  onConfigChanged: () => void
  onSessionsChanged: () => void
}

export function SettingsModal (
  {
    open, section, anchor, onSection, onClose, onMessage, onSignOut,
    onConfigChanged, onSessionsChanged
  }: Props
) {
  const body = useRef<HTMLDivElement>(null)
  const nav = useRef<HTMLElement>(null)

  useEffect(() => { window.WyckoffIcons?.render?.() })

  // 打开时禁用背景交互并把焦点移进来。
  useEffect(() => {
    const background = [...document.querySelectorAll<HTMLElement>(
      '.side, .thread, .pane-resizer, .pane'
    )]
    for (const node of background) node.inert = open
    if (open) requestAnimationFrame(() => nav.current?.querySelector<HTMLElement>('.dlg-n.on')?.focus())
    return () => { for (const node of background) node.inert = false }
  }, [open])

  /**
   * Esc 关闭 + Tab 环形困住焦点。
   *
   * inert 挡住了鼠标和背景的 Tab 目标，但从弹窗最后一个控件继续 Tab 会跳到
   * 浏览器地址栏之类的地方 —— 键盘用户就「掉出」了这个模态框。所以在两端
   * 显式回绕。
   */
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key !== 'Tab') return
      const dlg = nav.current?.closest('.dlg')
      if (!dlg) return
      const focusable = [...dlg.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      )].filter((n) => !n.hidden && n.getClientRects().length > 0)
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  /**
   * 把指定分栏滚到内容区顶部。
   *
   * 标题要等组件内部读完设置才出现，所以一帧不够 —— 有限次重试，
   * 超时就老实回到顶部（宁可停在开头，也不要因为 key 写错停在莫名其妙的位置）。
   */
  useEffect(() => {
    if (!open) return
    const box = body.current
    if (!box) return
    if (!anchor) { box.scrollTop = 0; return }
    let tries = 0
    let raf = 0
    const seek = () => {
      const head = box.querySelector<HTMLElement>(`.sec[data-sec-key="${anchor}"]`)
      if (!head) {
        if (tries++ < 20) { raf = requestAnimationFrame(seek); return }
        box.scrollTop = 0
        return
      }
      // 用 rect 差值而不是 offsetTop：不依赖布局层级。
      const delta = head.getBoundingClientRect().top - box.getBoundingClientRect().top
      const pad = parseFloat(getComputedStyle(head).marginTop) || 0
      box.scrollTop += delta - pad
    }
    seek()
    return () => cancelAnimationFrame(raf)
  }, [open, section, anchor])

  if (!open) return null

  return (
    <div className="ov" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dlg" role="dialog" aria-modal="true" aria-label={t('settings.title')}>
        <nav className="dlg-nav" ref={nav}>
          {SECTIONS.map(([sec, key]) => (
            <button
              key={sec}
              className={sec === section ? 'dlg-n on' : 'dlg-n'}
              type="button"
              aria-current={sec === section ? 'page' : 'false'}
              onClick={() => onSection(sec)}
            >
              {t(key)}
            </button>
          ))}
        </nav>
        <div className="dlg-main">
          <button className="dlg-x" type="button" aria-label={t('action.close')} onClick={onClose}>
            <i data-lucide="x" aria-hidden="true" />
          </button>
          <div className="dlg-body" ref={body}>
            <SettingsPanel
              section={section}
              onMessage={onMessage}
              onSignOut={onSignOut}
              onConfigChanged={onConfigChanged}
              onSessionsChanged={onSessionsChanged}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
