import { useEffect } from 'react'
import { useLocation } from 'react-router'
import { AlertTriangle, BarChart3, Bot, Briefcase, CalendarDays, CheckCircle2, CloudCog, Crown, Download, ExternalLink, GitBranch, MessageSquare, RadioTower, Rocket, Settings, Swords, Terminal, TrendingUp, Users, type LucideIcon } from 'lucide-react'
import { usePreferences, type Locale, type TranslationKey } from '@/lib/preferences'
import { useAuthStore } from '@/stores/auth'
import { usePlanetMembership } from '@/lib/planet-membership-gate'

const workflows = [
  {
    icon: MessageSquare,
    titleKey: 'guide.workflow.chat.title',
    descKey: 'guide.workflow.chat.desc',
  },
  {
    icon: BarChart3,
    titleKey: 'guide.workflow.analysis.title',
    descKey: 'guide.workflow.analysis.desc',
  },
  {
    icon: Swords,
    titleKey: 'guide.workflow.battle.title',
    descKey: 'guide.workflow.battle.desc',
  },
  {
    icon: Briefcase,
    titleKey: 'guide.workflow.portfolio.title',
    descKey: 'guide.workflow.portfolio.desc',
  },
] satisfies { icon: LucideIcon; titleKey: TranslationKey; descKey: TranslationKey }[]

const tools = [
  { nameKey: 'guide.tool.market', detailKey: 'guide.tool.market.detail' },
  { nameKey: 'guide.tool.tracking', detailKey: 'guide.tool.tracking.detail' },
  { nameKey: 'guide.tool.signal', detailKey: 'guide.tool.signal.detail' },
  { nameKey: 'guide.tool.export', detailKey: 'guide.tool.export.detail' },
  { nameKey: 'guide.tool.history', detailKey: 'guide.tool.history.detail' },
  { nameKey: 'guide.tool.model', detailKey: 'guide.tool.model.detail' },
] satisfies { nameKey: TranslationKey; detailKey: TranslationKey }[]

const playbooks = [
  {
    labelKey: 'guide.playbook.pre',
    icon: RadioTower,
    textKey: 'guide.playbook.pre.text',
  },
  {
    labelKey: 'guide.playbook.mid',
    icon: TrendingUp,
    textKey: 'guide.playbook.mid.text',
  },
  {
    labelKey: 'guide.playbook.review',
    icon: Download,
    textKey: 'guide.playbook.review.text',
  },
] satisfies { labelKey: TranslationKey; icon: LucideIcon; textKey: TranslationKey }[]

const capabilityCopy = {
  'zh-CN': {
    eyebrow: '能力边界',
    title: 'Web 是日常工作台，后台负责重任务',
    intro: '当前 Web 端已经覆盖读盘、单股、多股、持仓、跟踪和导出这些高频动作；全市场漏斗、回测、回刷和运维任务继续放在 GitHub Actions、CLI 或数据库后台，避免把长任务和敏感权限塞进浏览器。',
    webTitle: 'Web 端已经接入',
    webItems: ['单股 320 日日线结构图、价值快照、AI 报告与本地历史', '多股对抗的相对强弱、叠加/分图、价值面校准与本地历史', '持仓诊断支持数据库持仓和手动持仓，结果保存在当前浏览器', '星球会员形态跟踪、批量行情导出、模型和数据源配置'],
    gapTitle: '系统有，但不放在 Web 里主跑',
    whyTitle: '为什么不全塞进 Web',
    costLinkText: '成本详见：COST_MODEL.md',
    accessTitle: '当前入口',
  },
  'en-US': {
    eyebrow: 'Capability Map',
    title: 'The web UI is the daily desk; background jobs carry the heavy work',
    intro: 'The web UI now covers the high-frequency loops: reading, single-stock analysis, stock battle, portfolio diagnosis, tracking, and export. Full-market funnels, backtests, repricing, and maintenance stay in GitHub Actions, CLI, or database-side jobs instead of pushing long jobs and sensitive permissions into the browser.',
    webTitle: 'Covered by the web UI',
    webItems: ['Single-stock 320-day chart, value snapshot, AI report, and local history', 'Stock battle with relative strength, overlay/separate charts, value calibration, and local history', 'Portfolio diagnosis for database or manual positions, with browser-local result history', 'Planet-member pattern tracking, batch market-data export, model and data-source settings'],
    gapTitle: 'Available in the system, but not browser-first',
    whyTitle: 'Why not put everything in the browser',
    costLinkText: 'Cost details: COST_MODEL.md',
    accessTitle: 'Where to use them today',
  },
} satisfies Record<Locale, {
  eyebrow: string
  title: string
  intro: string
  webTitle: string
  webItems: string[]
  gapTitle: string
  whyTitle: string
  costLinkText: string
  accessTitle: string
}>

const capabilityGaps = {
  'zh-CN': [
    ['全市场漏斗任务', 'A股保留完整 AI/通知链路；港股、美股每日扫描只写跟踪表并产出 Actions artifact。'],
    ['LLM 输入预览与飞书产物', '每日审核输入、完整报告、文件/文档分发更适合由 Actions 产出，Web 只承接查询和轻量分析。'],
    ['回测与参数网格', '牛熊周期、TopN、止损/止盈/持仓天数等批量计算适合后台长任务，Web 目前只展示部分结果。'],
    ['信号生命周期与补价回刷', 'pending/survived/confirmed/expired、推荐表现回刷、30 个交易日保留、MFE/MAE 统计都在后台维护。'],
    ['CLI Agent 与本机文件流', 'CLI/TUI、长上下文 Agent、诊断导出、本机环境变量和文件产物不适合直接暴露给浏览器。'],
    ['Streamlit 历史页面', 'Streamlit MVP 已在 main 退场并归档到 release/streamlit；新能力默认进入 CF Pages、CLI、MCP 或 Actions。'],
    ['维护与数据库任务', '缓存清理、RLS/服务端密钥操作、日志 artifact、全量清库/回刷属于运维能力，Web 只保留安全入口。'],
  ],
  'en-US': [
    ['Full-market funnel jobs', 'A-share scans run the full AI/notification pipeline; HK and US scans write tracking tables and artifacts only.'],
    ['LLM input previews and Feishu artifacts', 'Daily review inputs, full reports, and file/doc distribution fit Actions better; the web UI handles querying and lightweight analysis.'],
    ['Backtests and parameter grids', 'Bull/bear windows, TopN, stop-loss, take-profit, and holding-day grids are long-running backend workloads.'],
    ['Signal lifecycle and repricing', 'pending/survived/confirmed/expired updates, recommendation repricing, 30-trading-day retention, and MFE/MAE stats run in background jobs.'],
    ['CLI Agent and local file flows', 'CLI/TUI, long-context agents, diagnostic exports, local env vars, and file artifacts should stay on the user machine.'],
    ['Archived Streamlit pages', 'The Streamlit MVP is retired from main and archived on release/streamlit; new work goes to CF Pages, CLI, MCP, or Actions.'],
    ['Maintenance and database jobs', 'Cache cleanup, service-role operations, log artifacts, full cleanup, and repricing are operational tools, not browser-first UI.'],
  ],
} satisfies Record<Locale, [string, string][]>

const capabilityReasons = {
  'zh-CN': ['全市场漏斗、回测网格、LLM 审核和回刷统计都是长时间计算；浏览器适合发起、查看和轻量分析，不适合作为任务队列。', '服务端密钥、RLS、批量清库和全量回刷有权限风险；这些动作留在 Actions/CLI/运维脚本里更可控。', 'Web 本地历史只保存在当前浏览器，是为了避免把用户临时分析结果写库；跨设备沉淀再单独做同步策略。'],
  'en-US': ['Full-market funnels, backtest grids, LLM reviews, and repricing stats are long-running computations; the browser is better for launching, viewing, and lightweight analysis than acting as a job queue.', 'Service-role keys, RLS, bulk cleanup, and full repricing carry permission risk, so they stay in Actions, CLI, or ops scripts.', 'Web local history is intentionally browser-local to avoid writing temporary analysis results to the database; cross-device sync needs a separate policy.'],
} satisfies Record<Locale, string[]>

const capabilityLaunch = {
  'zh-CN': {
    kicker: '正式开放',
    date: '2026-06-03',
    title: '「威科夫策略交流学习」知识星球',
    desc: '知识星球现已正式开放！年费 518 元/年（折合每天约 1.4 元）。项目本身始终保持开源，也欢迎 fork、自行部署、提交 Issue 与 PR。星球会员共享云端持仓、30 日形态跟踪、策略归因、隔离研究计算、手机遥控、每日漏斗产物和专属交流；个人模型与数据源 Key 仍由用户自行管理。',
    note: '费用主要用于共同平摊数据源、数据库、云服务器、AI API 和自动化任务等系统运维硬成本；不是投资顾问费，也不构成任何收益承诺。',
    badge: '星球会员特权',
    tags: ['云端持仓', '30 日跟踪', '策略归因', '隔离研究', '手机遥控', '每日漏斗'],
  },
  'en-US': {
    kicker: 'Now Open',
    date: '2026-06-03',
    title: 'Wyckoff Strategy Learning Planet',
    desc: 'Knowledge Planet is officially open at 518 CNY/year (about 1.4 CNY/day). The project remains open source and welcomes forks, issues, and PRs. Members share cloud portfolios, 30-day pattern tracking, strategy attribution, isolated research, phone remote control, daily funnel artifacts, and the private community. Personal model and data-source keys remain user-managed.',
    note: 'The fee mainly helps share hard operating costs such as data feeds, databases, cloud servers, AI APIs, and scheduled automation; it is not an investment advisory fee and does not imply any return guarantee.',
    badge: 'Member Benefits',
    tags: ['Cloud Portfolio', '30-Day Tracking', 'Attribution', 'Isolated Research', 'Phone Remote', 'Daily Funnel'],
  },
} satisfies Record<Locale, {
  kicker: string
  date: string
  title: string
  desc: string
  note: string
  badge: string
  tags: string[]
}>

const capabilityAccess = {
  'zh-CN': [
    ['GitHub Actions', '漏斗、审核输入、飞书产物、回测、回刷、维护任务'],
    ['本地 CLI', '准点触发、长任务、本地密钥、诊断导出和文件工作流'],
    ['运维脚本 / 数据库控制台', '偏后台的配置、调试和数据库维护能力'],
    ['Web 端', '读盘、单股、多股、持仓、跟踪、导出、配置和本地历史'],
  ],
  'en-US': [
    ['GitHub Actions', 'Funnel, review input, Feishu artifacts, backtest, repricing, maintenance jobs'],
    ['Local CLI', 'On-time triggers, long jobs, local secrets, diagnostic exports, and file workflows'],
    ['Ops scripts / database console', 'Admin-like configuration, debugging, and database maintenance'],
    ['Web UI', 'Reading, single-stock, battle, portfolio, tracking, export, settings, and local history'],
  ],
} satisfies Record<Locale, [string, string][]>

export function PlanetMembershipPage() {
  const { locale, t } = usePreferences()
  const location = useLocation()
  const user = useAuthStore((state) => state.user)
  const membership = usePlanetMembership(user?.id)

  useEffect(() => {
    if (location.hash !== '#capability-boundary') return
    requestAnimationFrame(() => document.getElementById('capability-boundary')?.scrollIntoView({ block: 'start' }))
  }, [location.hash])

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-8 p-6">
      <header className="border-b border-border pb-5">
        <div className="flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <Bot size={22} />
          </span>
          <div>
            <h1 className="text-xl font-semibold">{t('guide.title')}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {t('guide.subtitle')}
            </p>
          </div>
        </div>
      </header>

      <PlanetMembershipOverview
        email={user?.email || ''}
        isActive={membership.data?.isActive === true}
        expiresOn={membership.data?.expiresOn ?? null}
        hasError={membership.isError}
        isLoading={membership.isLoading}
        locale={locale}
      />

      <section>
        <div className="mb-4 flex items-end justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold">{t('guide.core')}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{t('guide.coreDesc')}</p>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {workflows.map(({ icon: Icon, titleKey, descKey }) => (
            <article key={titleKey} className="rounded-lg border border-border bg-background p-4 shadow-sm shadow-primary/5">
              <Icon className="mb-3 text-primary" size={20} />
              <h3 className="text-sm font-semibold">{t(titleKey)}</h3>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">{t(descKey)}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1fr_380px]">
        <div>
          <h2 className="mb-4 text-base font-semibold">{t('guide.modules')}</h2>
          <div className="overflow-hidden rounded-lg border border-border bg-background">
            <table className="w-full text-sm">
              <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('guide.module')}</th>
                  <th className="px-4 py-3 font-medium">{t('guide.description')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {tools.map((tool) => (
                  <tr key={tool.nameKey}>
                    <td className="whitespace-nowrap px-4 py-3 font-medium">{t(tool.nameKey)}</td>
                    <td className="px-4 py-3 leading-6 text-muted-foreground">{t(tool.detailKey)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <aside>
          <h2 className="mb-4 text-base font-semibold">{t('guide.rhythm')}</h2>
          <div className="space-y-3">
            {playbooks.map(({ icon: Icon, labelKey, textKey }) => (
              <div key={labelKey} className="rounded-lg border border-border bg-sidebar p-4">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Icon size={16} className="text-primary" />
                  {t(labelKey)}
                </div>
                <p className="mt-2 text-sm leading-6 text-muted-foreground">{t(textKey)}</p>
              </div>
            ))}
          </div>
        </aside>
      </section>

      <CapabilityBoundarySection locale={locale} />

      <section className="rounded-lg border border-border bg-primary/5 p-5">
        <div className="flex items-start gap-3">
          <Settings className="mt-0.5 text-primary" size={18} />
          <div>
            <h2 className="text-sm font-semibold">{t('guide.configEntry')}</h2>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              {t('guide.configDesc')}
            </p>
          </div>
        </div>
      </section>
    </div>
  )
}

function PlanetMembershipOverview({
  email,
  isActive,
  expiresOn,
  hasError,
  isLoading,
  locale,
}: {
  email: string
  isActive: boolean
  expiresOn: string | null
  hasError: boolean
  isLoading: boolean
  locale: Locale
}) {
  const zh = locale === 'zh-CN'
  const benefits = zh
    ? ['最近 30 个复盘交易日的形态跟踪', '策略归因与信号分层报告', '云端持仓保存与多端同步', '隔离 Python 研究计算', '手机遥控桌面读盘室', '每日漏斗产物与星球交流']
    : ['30 recent review trading days', 'Strategy attribution reports', 'Cloud portfolio sync', 'Isolated Python research', 'Phone remote for the desktop', 'Daily funnel artifacts and community']
  const status = isLoading ? (zh ? '正在核验' : 'Checking') : hasError ? (zh ? '会员状态暂时无法核验' : 'Membership check unavailable') : isActive ? (zh ? '星球会员已生效' : 'Planet membership active') : (zh ? '当前账号未开通' : 'Membership not active')
  const expiry = isActive ? (expiresOn || (zh ? '长期有效' : 'No expiry')) : '—'
  return (
    <section className="overflow-hidden rounded-2xl border border-amber-300/40 bg-gradient-to-br from-amber-500/10 via-background to-violet-500/10 p-5">
      <div className="grid gap-5 lg:grid-cols-[320px_1fr]">
        <div className="rounded-xl border border-border bg-background/85 p-4">
          <div className="flex items-center gap-2 text-sm font-semibold"><Crown size={18} className="text-amber-500" />{status}</div>
          <dl className="mt-4 space-y-3 text-sm">
            <div><dt className="text-xs text-muted-foreground">{zh ? '当前账号' : 'Account'}</dt><dd className="mt-1 break-all font-medium">{email || '—'}</dd></div>
            <div><dt className="text-xs text-muted-foreground">{zh ? '有效期' : 'Valid through'}</dt><dd className="mt-1 font-medium">{expiry}</dd></div>
          </dl>
          <p className="mt-4 text-xs leading-5 text-muted-foreground">{zh ? '会员身份与数据源、模型配置分别核验；开通会员不会自动写入你的私人 API Key。' : 'Membership and API configuration are checked separately. Membership never writes private API keys for you.'}</p>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-primary">{zh ? '星球会员专属能力' : 'Planet member benefits'}</p>
          <h2 className="mt-2 text-xl font-semibold">{zh ? '把个人部署里最麻烦的共享能力交给云端' : 'Shared cloud capabilities without operating them alone'}</h2>
          <div className="mt-4 grid gap-2 sm:grid-cols-2">
            {benefits.map((item) => <div key={item} className="flex items-start gap-2 rounded-lg bg-background/70 p-3 text-sm"><CheckCircle2 size={16} className="mt-0.5 shrink-0 text-primary" /><span>{item}</span></div>)}
          </div>
          <p className="mt-4 text-xs leading-5 text-muted-foreground">{zh ? '普通用户仍可自配模型和数据源使用读盘、单股、多股、手动持仓诊断、导出与本地历史。会员费用用于分摊共享系统成本，不是投资顾问费，也不承诺收益。' : 'Everyone can still use reading, analysis, battles, manual portfolio diagnosis, export, and local history with their own model and data source. Membership shares system costs; it is not investment advice and makes no return promise.'}</p>
        </div>
      </div>
    </section>
  )
}

function CapabilityBoundarySection({ locale }: { locale: Locale }) {
  const copy = capabilityCopy[locale]
  return (
    <section id="capability-boundary" className="scroll-mt-6 overflow-hidden rounded-2xl border border-border bg-gradient-to-br from-sidebar via-background to-primary/5 p-5 shadow-sm">
      <div className="mb-5 max-w-3xl">
        <p className="text-xs font-semibold uppercase tracking-[0.24em] text-primary">{copy.eyebrow}</p>
        <h2 className="mt-2 text-2xl font-semibold tracking-tight">{copy.title}</h2>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">{copy.intro}</p>
      </div>
      <CapabilityLaunchBanner locale={locale} />
      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.4fr]">
        <CapabilityWebCard copy={copy} />
        <CapabilityGapCard title={copy.gapTitle} locale={locale} />
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <CapabilityListCard icon={AlertTriangle} title={copy.whyTitle} items={capabilityReasons[locale]} costLinkText={copy.costLinkText} tone="warning" />
        <CapabilityAccessCard title={copy.accessTitle} locale={locale} />
      </div>
    </section>
  )
}

function CapabilityLaunchBanner({ locale }: { locale: Locale }) {
  const copy = capabilityLaunch[locale]
  return (
    <article className="relative mb-5 overflow-hidden rounded-2xl border border-amber-300/50 bg-[radial-gradient(circle_at_top_left,rgba(245,158,11,0.26),transparent_36%),linear-gradient(135deg,rgba(15,23,42,0.96),rgba(88,28,135,0.85)_48%,rgba(180,83,9,0.88))] p-5 text-white shadow-lg shadow-amber-900/10">
      <div className="absolute -right-10 -top-10 h-36 w-36 rounded-full border border-white/20 bg-white/10 blur-sm" />
      <div className="relative grid gap-5 lg:grid-cols-[1fr_300px] lg:items-center">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded-full border border-white/20 bg-white/15 px-3 py-1 text-xs font-semibold backdrop-blur">
              <Rocket size={13} />
              {copy.kicker}
            </span>
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-300 px-3 py-1 text-xs font-bold text-slate-950">
              <CalendarDays size={13} />
              {copy.date}
            </span>
          </div>
          <h3 className="mt-4 text-2xl font-semibold tracking-tight">{copy.title}</h3>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-white/90">{copy.desc}</p>
          <p className="mt-3 flex max-w-3xl items-start gap-2 rounded-lg border border-white/15 bg-white/10 px-3 py-2 text-xs leading-5 text-white/85">
            <AlertTriangle className="mt-0.5 shrink-0" size={14} />
            <span>{copy.note}</span>
          </p>
        </div>
        <div className="rounded-2xl border border-white/15 bg-white/10 p-4 backdrop-blur flex flex-col items-center gap-4 sm:flex-row lg:flex-col lg:items-center">
          <div className="w-full">
            <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
              <Users size={16} />
              {copy.badge}
            </div>
            <div className="flex flex-wrap gap-1.5 mb-1">
              {copy.tags.map((tag) => (
                <span key={tag} className="rounded-full bg-white/15 px-2.5 py-0.5 text-xs text-white/90">
                  {tag}
                </span>
              ))}
            </div>
          </div>
          <div className="flex flex-col items-center rounded-xl bg-white p-2 shadow-md shrink-0 w-48 transition-transform hover:scale-[1.03] duration-200">
            <img src="/zsxq_qr.jpg" alt="Knowledge Planet QR" className="w-full h-auto object-contain rounded-lg" />
            <span className="mt-2 text-xs font-bold text-slate-800">微信扫码 加入星球</span>
          </div>
        </div>
      </div>
    </article>
  )
}

function CapabilityWebCard({ copy }: { copy: (typeof capabilityCopy)[Locale] }) {
  return (
    <article className="rounded-xl border border-primary/20 bg-primary/10 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-primary">
        <CheckCircle2 size={17} />
        {copy.webTitle}
      </div>
      <ul className="space-y-2 text-sm leading-6 text-muted-foreground">
        {copy.webItems.map((item) => <li key={item}>• {item}</li>)}
      </ul>
    </article>
  )
}

function CapabilityGapCard({ title, locale }: { title: string; locale: Locale }) {
  return (
    <article className="rounded-xl border border-border bg-background/80 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <CloudCog size={17} className="text-primary" />
        {title}
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        {capabilityGaps[locale].map(([name, desc]) => (
          <div key={name} className="rounded-lg border border-border bg-sidebar/70 p-3">
            <h3 className="text-sm font-semibold">{name}</h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">{desc}</p>
          </div>
        ))}
      </div>
    </article>
  )
}

function CapabilityListCard({ icon: Icon, title, items, costLinkText, tone }: { icon: LucideIcon; title: string; items: string[]; costLinkText: string; tone: 'warning' }) {
  return (
    <article className="rounded-xl border border-border bg-background/80 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <Icon size={17} className={tone === 'warning' ? 'text-warning' : 'text-primary'} />
        {title}
      </div>
      <ul className="space-y-2 text-sm leading-6 text-muted-foreground">
        {items.map((item) => <li key={item}>• {item}</li>)}
      </ul>
      <a
        className="mt-3 inline-flex items-center gap-1 rounded-full border border-warning/30 bg-warning/10 px-3 py-1.5 text-xs font-medium text-warning transition hover:bg-warning/15"
        href="https://github.com/YoungCan-Wang/WyckoffTradingAgent/blob/main/docs/COST_MODEL.md"
        rel="noreferrer"
        target="_blank"
      >
        {costLinkText}
        <ExternalLink size={13} />
      </a>
    </article>
  )
}

function CapabilityAccessCard({ title, locale }: { title: string; locale: Locale }) {
  const icons = [GitBranch, Terminal, Settings, Bot]
  return (
    <article className="rounded-lg border border-border bg-background p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <GitBranch size={17} className="text-primary" />
        {title}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {capabilityAccess[locale].map(([name, desc], index) => {
          const Icon = icons[index] ?? Bot
          return (
            <div key={name} className="rounded-md bg-muted/60 p-3">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Icon size={15} className="text-primary" />
                {name}
              </div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{desc}</p>
            </div>
          )
        })}
      </div>
    </article>
  )
}
