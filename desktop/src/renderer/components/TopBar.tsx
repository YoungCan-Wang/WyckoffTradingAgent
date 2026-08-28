/**
 * 顶栏：侧栏开关槽位 + 当前视图标题 + 后端状态 + 「打开」菜单。
 *
 * 后端健康是默认预期，所以从不宣告；重启按钮只在**不健康**时出现 ——
 * 那才是用户能采取行动的时刻。
 *
 * 原来这里有个「待审 N」的角标。写操作的确认已经回到对话里当场问，没有待办
 * 可数了 —— 留一个恒为 0 的计数只会让人以为别处还藏着一个收件箱。
 */
import { useEffect, useRef } from 'react'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  titleKey: string
  backendState: string
  onRestart: () => void
  onOpenMenu: (anchor: HTMLElement) => void
  toggleSlot: React.ReactNode
}

export function TopBar (
  { titleKey, backendState, onRestart, onOpenMenu, toggleSlot }: Props
) {
  const root = useRef<HTMLElement>(null)
  useEffect(() => { window.WyckoffIcons?.render?.() })

  const ready = backendState === 'ready'

  return (
    <header className="ttop" ref={root}>
      <span className="side-toggle-slot">{toggleSlot}</span>
      <div className="ttitle">{t(titleKey)}</div>
      <div className="tools">
        {!ready ? (
          <button
            className={`icb has-dot ${backendState}`}
            type="button"
            title={t('tooltip.restart')}
            onClick={onRestart}
          >
            <i data-lucide="refresh-cw" aria-hidden="true" />
          </button>
        ) : null}
        <button
          className="topbtn"
          type="button"
          aria-haspopup="menu"
          onClick={(e) => onOpenMenu(e.currentTarget)}
        >
          <i className="topbtn-g" data-lucide="plus" aria-hidden="true" />
          <span>{t('menu.open')}</span>
          <i className="topbtn-cv" data-lucide="chevron-down" aria-hidden="true" />
        </button>
      </div>
    </header>
  )
}
