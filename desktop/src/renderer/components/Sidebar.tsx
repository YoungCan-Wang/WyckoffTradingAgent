/**
 * 侧栏：导航 + 账号行。
 *
 * 图标用 lucide 的 data-lucide 属性，由 lucide 在挂载后替换成 svg ——
 * 与其余静态骨架一致，不引入第二套图标方案。
 */
import { useEffect, useRef } from 'react'

const t = (key: string) => window.WyckoffI18n.t(key)

export interface NavItem {
  view: string
  icon: string
  labelKey: string
  /** 侧栏右侧的计数（审批、计划任务）。 */
  badge?: string
  badgeWarn?: boolean
}

const GROUPS: Array<{ labelKey: string; items: NavItem[] }> = [
  {
    labelKey: 'nav.groupWork',
    items: [
      { view: 'chat', icon: 'sparkles', labelKey: 'nav.today' },
      { view: 'tasks', icon: 'list-checks', labelKey: 'nav.tasks' },
      { view: 'approvals', icon: 'shield-check', labelKey: 'nav.approvals' }
    ]
  },
  {
    labelKey: 'nav.groupDomain',
    items: [
      { view: 'portfolio', icon: 'briefcase', labelKey: 'nav.portfolio' },
      { view: 'schedules', icon: 'calendar-clock', labelKey: 'nav.schedules' },
      { view: 'tracking', icon: 'radar', labelKey: 'nav.tracking' },
      { view: 'attribution', icon: 'git-branch', labelKey: 'nav.attribution' }
    ]
  },
  {
    labelKey: 'nav.groupLibrary',
    items: [{ view: 'reports', icon: 'file-chart-column-increasing', labelKey: 'nav.reports' }]
  }
]

interface Props {
  active: string
  onNavigate: (view: string) => void
  counts: { approvals: number; schedules: number }
  accountLabel: string
  accountInitial: string
  onAccountClick: (anchor: HTMLElement) => void
  /** 侧栏展开时开关按钮住在这里；收起时住在顶栏。 */
  toggleSlot: React.ReactNode
}

export function Sidebar (
  { active, onNavigate, counts, accountLabel, accountInitial, onAccountClick, toggleSlot }: Props
) {
  const root = useRef<HTMLElement>(null)
  // lucide 只替换当前 DOM 里的占位符，所以每次结构变化后要再跑一次。
  useEffect(() => { window.WyckoffIcons?.render?.() })

  const badgeFor = (view: string) => {
    if (view === 'approvals' && counts.approvals) return String(counts.approvals)
    if (view === 'schedules' && counts.schedules) return String(counts.schedules)
    return ''
  }

  return (
    <aside className="side" id="side" ref={root}>
      <div className="side-titlebar">
        <div className="brand">
          Wyckoff <i className="cv" data-lucide="chevron-down" aria-hidden="true" />
        </div>
        <span className="side-toggle-slot">{toggleSlot}</span>
      </div>

      <button className="side-new" type="button" onClick={() => onNavigate('chat')}>
        {t('nav.newAnalysis')}
      </button>

      <nav className="nav" aria-label={t('nav.primary')}>
        {GROUPS.map((g) => (
          <div className="nav-group" key={g.labelKey}>
            <div className="nav-label">{t(g.labelKey)}</div>
            {g.items.map((item) => {
              const badge = badgeFor(item.view)
              return (
                <button
                  key={item.view}
                  className={item.view === active ? 'nv on' : 'nv'}
                  type="button"
                  onClick={() => onNavigate(item.view)}
                >
                  <i className="nav-icon" data-lucide={item.icon} aria-hidden="true" />
                  <span>{t(item.labelKey)}</span>
                  {/* 待批准是花钱的决定，用强调色 */}
                  {badge ? (
                    <span className={item.view === 'approvals' ? 'n warn' : 'n'}>{badge}</span>
                  ) : null}
                </button>
              )
            })}
          </div>
        ))}
      </nav>

      <div className="side-foot">
        <button
          className="acct"
          type="button"
          aria-haspopup="menu"
          onClick={(e) => onAccountClick(e.currentTarget)}
        >
          <span className="ava">{accountInitial}</span>
          <span className="acct-n">{accountLabel}</span>
          <i className="acct-cv" data-lucide="chevron-down" aria-hidden="true" />
        </button>
      </div>
    </aside>
  )
}
