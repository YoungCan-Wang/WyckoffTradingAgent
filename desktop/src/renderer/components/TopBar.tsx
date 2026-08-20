/**
 * 顶栏：侧栏开关槽位 + 当前视图标题 + 待批提示 + 后端状态 + 「打开」菜单。
 *
 * 后端健康是默认预期，所以从不宣告；重启按钮只在**不健康**时出现 ——
 * 那才是用户能采取行动的时刻。
 */
import { useEffect, useRef } from 'react'

const t = (key: string, params?: Record<string, string | number>) => window.WyckoffI18n.t(key, params)

interface Props {
  titleKey: string
  pendingCount: number
  backendState: string
  onPendingClick: () => void
  onRestart: () => void
  onOpenMenu: (anchor: HTMLElement) => void
  toggleSlot: React.ReactNode
}

export function TopBar (
  { titleKey, pendingCount, backendState, onPendingClick, onRestart, onOpenMenu, toggleSlot }: Props
) {
  const root = useRef<HTMLElement>(null)
  useEffect(() => { window.WyckoffIcons?.render?.() })

  const ready = backendState === 'ready'

  return (
    <header className="ttop" ref={root}>
      <span className="side-toggle-slot">{toggleSlot}</span>
      <div className="ttitle">{t(titleKey)}</div>
      <div className="tools">
        {/* 待批准是花钱的决定；侧栏默认收起时那个角标看不见，所以在常驻的
            顶栏也放一个。 */}
        {pendingCount ? (
          <button className="chip" type="button" title={t('tooltip.pending')} onClick={onPendingClick}>
            {t('approvals.tierReview')} {pendingCount}
          </button>
        ) : null}
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
