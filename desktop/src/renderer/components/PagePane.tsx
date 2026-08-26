/**
 * 目标页面容器：标题 + 副标题 + 内容。
 *
 * 这些是「导航过去」的地方，所以占据主区；右侧面板留给 agent 产出的东西。
 * 报告库仍由命令式的 artifact viewer 渲染（sandbox iframe 是安全边界，
 * 不该由 React 接管），所以它走一个挂载点。
 */
import { useEffect, useRef } from 'react'
import { ApprovalsPage } from './ApprovalsPage'
import { SchedulesPage } from './SchedulesPage'
import { PortfolioPage } from './PortfolioPage'
import { TrackingPage } from './TrackingPage'
import { AttributionPage } from './AttributionPage'

const t = (key: string) => window.WyckoffI18n.t(key)

interface Spec {
  titleKey: string
  subKey: string
  /** 表格类页面要更宽的内容区。 */
  wide?: boolean
}

const SPECS: Record<string, Spec> = {
  approvals: { titleKey: 'nav.approvals', subKey: 'approvals.pageSub' },
  schedules: { titleKey: 'schedules.heading', subKey: 'schedules.pageSub' },
  portfolio: { titleKey: 'tab.charts', subKey: 'portfolio.pageSub', wide: true },
  tracking: { titleKey: 'tracking.heading', subKey: 'tracking.pageSub', wide: true },
  attribution: { titleKey: 'attribution.heading', subKey: 'attribution.pageSub' },
  reports: { titleKey: 'nav.reports', subKey: 'reports.pageSub', wide: true }
}

export function PagePane ({ view }: { view: string }) {
  const spec = SPECS[view]
  if (!spec) return null

  return (
    <section className={spec.wide ? 'page wide' : 'page'}>
      <div className="page-in">
        <h1 className="page-t">{t(spec.titleKey)}</h1>
        <p className="page-s">{t(spec.subKey)}</p>
        <div id="page-body">
          {view === 'approvals' ? <ApprovalsPage /> : null}
          {view === 'schedules' ? <SchedulesPage /> : null}
          {view === 'portfolio' ? <PortfolioPage /> : null}
          {view === 'tracking' ? <TrackingPage /> : null}
          {view === 'attribution' ? <AttributionPage /> : null}
          {view === 'reports' ? <ReportsHost /> : null}
        </div>
      </div>
    </section>
  )
}

/**
 * 报告库：命令式 viewer 的挂载点。
 *
 * 它把 AI 生成的 HTML 放进 sandbox iframe（无脚本、无同源）—— 那是安全边界，
 * 用 React 重写等于把这个边界重做一遍，没有收益。
 */
function ReportsHost () {
  const host = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const node = host.current
    if (!node) return
    let dispose: (() => void) | undefined
    let alive = true
    void (async () => {
      const built = await window.WyckoffShell?.buildReportPage?.()
      if (!alive || !built || !node) {
        built?.dispose?.()
        return
      }
      node.replaceChildren(built.node)
      dispose = built.dispose
    })()
    return () => {
      alive = false
      if (dispose) dispose()
    }
  }, [])

  return <div className="report-page" ref={host}><p className="empty">{t('tab.loading')}</p></div>
}
