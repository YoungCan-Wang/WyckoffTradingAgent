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
  /** 侧栏右侧的计数（计划任务）。 */
  badge?: string
  badgeWarn?: boolean
}

const GROUPS: Array<{ labelKey: string; items: NavItem[] }> = [
  {
    labelKey: 'nav.groupWork',
    // 这里**没有**指向 chat 视图的导航项。对话不是一个和「审批」并列的目的地，
    // 它是这个应用的主界面 —— 用顶部「新建分析」开新的，用下面的会话列表回到
    // 已有的。放一个「今天」在这里会和会话列表抢同一件事，而且那个名字并不按
    // 日期筛任何东西，点进去看到的可能是上周的会话。
    //
    // 也没有「任务运行」。它读的是几个接口的汇总，每一行的按钮都只是「去某个
    // 页面」—— 自己不能改任何东西,那些计数已经由下面的徽标和各自页面给出。
    //
    // 「确认记录」不是原来的「审批」页。写操作的确认已经回到对话里当场问
    // （见 ConfirmCardInline）,这里只留一份只读流水,所以它和「定时任务」一样
    // 是事后来看的东西,没有徽标 —— 没有待办可数。
    items: [
      { view: 'records', icon: 'shield-check', labelKey: 'records.heading' },
      { view: 'schedules', icon: 'calendar-clock', labelKey: 'nav.schedules' }
    ]
  },
  {
    labelKey: 'nav.groupDomain',
    // 剩下三项是「看数据」,不需要你动手。
    items: [
      { view: 'portfolio', icon: 'briefcase', labelKey: 'nav.portfolio' },
      { view: 'tracking', icon: 'radar', labelKey: 'nav.tracking' },
      { view: 'attribution', icon: 'git-branch', labelKey: 'nav.attribution' },
      // 报告原来自成一个「资料库」分组。同样是一项撑一个标题 —— 归到「业务」
      // 里,它和跟踪/归因一样是只读的看数据。
      { view: 'reports', icon: 'file-chart-column-increasing', labelKey: 'nav.reports' }
    ]
  }
]

interface Props {
  active: string
  onNavigate: (view: string) => void
  onNewAnalysis: () => void
  counts: { schedules: number }
  accountLabel: string
  accountInitial: string
  onAccountClick: (anchor: HTMLElement) => void
  /** 侧栏展开时开关按钮住在这里；收起时住在顶栏。 */
  toggleSlot: React.ReactNode
  /** 会话区。作为插槽传进来而不是在这里组装 —— 会话状态属于 App。 */
  sessionSlot?: React.ReactNode
}

export function Sidebar (
  {
    active, onNavigate, onNewAnalysis, counts, accountLabel, accountInitial,
    onAccountClick, toggleSlot, sessionSlot
  }: Props
) {
  const root = useRef<HTMLElement>(null)
  // lucide 只替换当前 DOM 里的占位符，所以每次结构变化后要再跑一次。
  useEffect(() => { window.WyckoffIcons?.render?.() })

  const badgeFor = (view: string) => {
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

      <button className="side-new" type="button" onClick={onNewAnalysis}>
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
                  /* 与语言无关的选择器 —— 测试和探针都不该依赖译文 */
                  data-view={item.view}
                  onClick={() => onNavigate(item.view)}
                >
                  <i className="nav-icon" data-lucide={item.icon} aria-hidden="true" />
                  <span>{t(item.labelKey)}</span>
                  {badge ? <span className="n">{badge}</span> : null}
                </button>
              )
            })}
          </div>
        ))}
      </nav>

      {/* 会话列表在固定入口下方，自己一个滚动区 —— 固定入口不该被会话挤出视野 */}
      {sessionSlot}

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
