/**
 * React 挂载点。迁移期间与 vanilla 共存：app.js 仍拥有窗口、对话流和其余
 * 面板，只有已迁移的屏交给 React。
 *
 * 暴露 window.WyckoffReact.mountSettings(host, section) 供 app.js 调用。
 * root 缓存在 host 上并复用 —— 每次打开设置都 createRoot 会泄漏 root，
 * 表现为切换几次后事件被处理多次。
 */
import type { ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import {
  ArrowUp,
  Briefcase,
  CalendarClock,
  ChartCandlestick,
  ChevronDown,
  createIcons,
  FileText,
  FileChartColumnIncreasing,
  FolderOpen,
  GitBranch,
  Globe2,
  ListChecks,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Radar,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sparkles,
  X
} from 'lucide'
import { SettingsPanel } from './components/SettingsPanel'
import { TrackingPage } from './components/TrackingPage'
import { AttributionPage } from './components/AttributionPage'
import { ApprovalsPage } from './components/ApprovalsPage'
import { TasksPage } from './components/TasksPage'
import { SchedulesPage } from './components/SchedulesPage'
import { ChatView } from './components/ChatView'
import { App } from './components/App'
import { PortfolioPage } from './components/PortfolioPage'
import { clearAllCaches, isPortfolioWriteTool } from './lib/portfolioCache'

// 与 Web 端共用 Lucide 的细线语言。只传桌面 chrome 实际使用的图标，避免整套进入包。
const ICONS = {
  ArrowUp,
  Briefcase,
  CalendarClock,
  ChartCandlestick,
  ChevronDown,
  FileText,
  FileChartColumnIncreasing,
  FolderOpen,
  GitBranch,
  Globe2,
  ListChecks,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  Radar,
  RefreshCw,
  Settings,
  ShieldCheck,
  Sparkles,
  X
}

const refreshIcons = () => createIcons({ icons: ICONS })
refreshIcons()
/**
 * 组件挂载后要再跑一次：lucide 只替换调用那一刻 DOM 里的占位符，
 * React 后来插入的 data-lucide 不会自动变成 svg。
 */
window.WyckoffIcons = { render: refreshIcons }

const roots = new WeakMap<Element, Root>()

/**
 * app.js 传进来的回调。系统消息要进对话流、退出登录要刷新侧栏并关掉面板，
 * 这些 DOM 还不属于 React，所以由它继续拥有。
 */
export interface SettingsHooks {
  onMessage: (text: string, isError?: boolean) => void
  onSignOut: () => void
  /** 配置变了要通知 app.js：输入框的模型选择器还在它手里。 */
  onConfigChanged: () => void
}

let hooks: SettingsHooks = {
  onMessage: () => {},
  onSignOut: () => {},
  onConfigChanged: () => {}
}

function setHooks (next: Partial<SettingsHooks>) {
  hooks = { ...hooks, ...next }
}

function mountSettings (host: HTMLElement, section: string) {
  let root = roots.get(host)
  if (!root) {
    root = createRoot(host)
    roots.set(host, root)
  }
  root.render(
    <SettingsPanel
      section={section}
      onMessage={hooks.onMessage}
      onSignOut={hooks.onSignOut}
      onConfigChanged={hooks.onConfigChanged}
    />
  )
}

/** 交还给 vanilla 渲染前必须卸载，否则 React 和手写 DOM 会抢同一个容器。 */
function unmountSettings (host: HTMLElement) {
  const root = roots.get(host)
  if (root) {
    root.unmount()
    roots.delete(host)
  }
}

/**
 * 挂载一个整页视图，返回 app.js 的 PAGES 约定形状 `{ node, dispose }`。
 *
 * 与设置面板不同：这里每次都新建容器和 root，由 showPage 通过 dispose 回收。
 * 复用 root 反而危险 —— 页面切换会换掉整个容器节点，而旧 root 仍指向已被
 * replaceChildren 摘掉的 DOM，下一次 render 会报 NotFoundError。
 *
 * dispose 里延后一帧卸载：unmount 不能在 React 自己的渲染周期内同步调用，
 * 否则 React 会警告「正在渲染时卸载」。
 */
function mountPage (element: ReactNode): { node: HTMLElement; dispose: () => void } {
  const node = document.createElement('div')
  node.className = 'react-page'
  const root = createRoot(node)
  root.render(element)
  return {
    node,
    dispose: () => { setTimeout(() => root.unmount(), 0) }
  }
}

declare global {
  interface Window {
    WyckoffReact: {
      mountSettings: typeof mountSettings
      unmountSettings: typeof unmountSettings
      /** app.js 用它判断某个 section 是否已经迁到 React。 */
      handles: (section: string) => boolean
      /** app.js 注入它仍拥有的动作（系统消息、退出登录）。 */
      setHooks: typeof setHooks
      /** 整页视图：返回 PAGES 约定的 { node, dispose }。 */
      trackingPage: () => { node: HTMLElement; dispose: () => void }
      attributionPage: () => { node: HTMLElement; dispose: () => void }
      portfolioPage: () => { node: HTMLElement; dispose: () => void }
      approvalsPage: () => { node: HTMLElement; dispose: () => void }
      tasksPage: () => { node: HTMLElement; dispose: () => void }
      schedulesPage: () => { node: HTMLElement; dispose: () => void }
      mountChat: (host: HTMLElement) => () => void
      /** 对话里跑了改持仓的工具时调用，作废本地缓存。 */
      invalidatePortfolioCache: (toolName: string) => void
      /** 登录态变化时调用：清掉所有账号的持仓缓存。 */
      clearPortfolioCaches: () => void
    }
    WyckoffRefreshIcons: () => void
    /**
     * app.js 停放 hooks 的地方。两个脚本的执行顺序由 type="module" 决定，
     * 不能靠「另一方已就绪」的假设来接线。
     */
    WyckoffPendingHooks?: Partial<SettingsHooks>
    /**
     * 仍归 app.js 拥有的动作（路由、侧栏计数、开图）。React 页面通过它回调，
     * 等 app.js 拆完这个桥也会一起消失。
     */
    WyckoffApp?: {
      navigate?: (view: string) => void
      refreshApprovals?: () => void
      refreshSchedules?: () => void
      openKline?: (code: string) => void
      /** 把报告送去产物面板。 */
      openReport?: (title: string, body: string) => void
      /** 这一轮画过标注的图要再刷一次 —— 标注写在图建好之后。 */
      refreshCharts?: (codes: string[]) => void
      /** 打开设置并滚到某一栏。 */
      openSettings?: (section: string, anchor?: string) => void
      /** 系统提示行写进对话流。 */
      sysLine?: (text: string, isError?: boolean) => void
      /** 「回车发送」设置项的当前值。 */
      getSendOnEnter?: () => boolean
    }
    /** React 侧暴露给 app.js 的对话入口（反向）。 */
    WyckoffChat?: { sysLine: (text: string, isError?: boolean) => void }
    /** app.js 停放会话区容器的地方。 */
    WyckoffPendingChatHost?: HTMLElement
    /** 把新插入的 data-lucide 占位符换成 svg。 */
    WyckoffIcons?: { render: () => void }
    /**
     * 命令式外壳（shell.js）：产物面板、K 线、内置浏览器、外观。
     * 这些不用 React —— canvas 绘图、原生 view 几何、sandbox iframe 都是
     * 命令式的，套一层壳不会让代码更好。
     */
    WyckoffShell?: {
      openReport?: (title: string, body: string) => void
      openKline?: (symbol: string) => void
      refreshCharts?: (codes: string[]) => void
      /** anchor：触发菜单的按钮，浮层定位到它下方。 */
      promptKline?: (anchor?: HTMLElement) => void
      openBrowser?: () => void
      togglePane?: () => void
      syncBrowser?: () => void
      getSendOnEnter?: () => boolean
      buildReportPage?: () => Promise<{ node: HTMLElement; dispose?: () => void }>
      loadAppearance?: () => Promise<void>
    }
  }
}

// 设置面板三页全部迁完。等对话流等其余界面也搬完，这个集合和 app.js 里的
// 分派分支就可以一起删掉。
const MIGRATED = new Set(['general', 'agent', 'account'])

window.WyckoffReact = {
  mountSettings,
  unmountSettings,
  setHooks,
  handles: (section: string) => MIGRATED.has(section),
  trackingPage: () => mountPage(<TrackingPage />),
  attributionPage: () => mountPage(<AttributionPage />),
  portfolioPage: () => mountPage(<PortfolioPage />),
  approvalsPage: () => mountPage(<ApprovalsPage />),
  tasksPage: () => mountPage(<TasksPage />),
  schedulesPage: () => mountPage(<SchedulesPage />),
  /** 会话区（欢迎页 + 对话流 + 输入区）。挂在 #stream 里，长驻不卸载。 */
  mountChat: (host: HTMLElement) => {
    const root = createRoot(host)
    root.render(<ChatView />)
    return () => root.unmount()
  },
  invalidatePortfolioCache: (toolName: string) => {
    // 全清而不是只清当前账号：这里拿不到 user_id（同步调用），而缓存本来就是
    // 可丢的。多清的代价是别的账号下次进页面重拉一次。
    if (isPortfolioWriteTool(toolName)) clearAllCaches()
  },
  clearPortfolioCaches: clearAllCaches
}
window.WyckoffRefreshIcons = refreshIcons

// app.js 是普通脚本，会在这个 type="module" 之前跑完 —— 它那边的
// `if (window.WyckoffReact)` 恒为假。所以由后就绪的一方（就是这里）主动取：
// app.js 把 hooks 挂在 WyckoffPendingHooks 上等着。
//
// 漏了这一步的后果是静默的：设置页「退出登录」点了没反应，模型改了输入区的
// 选择器不刷新，因为 React 一直在调默认的空函数。
if (window.WyckoffPendingHooks) {
  setHooks(window.WyckoffPendingHooks)
}

// app.js 先跑完时会把会话区容器停在这里等 React 来接（同 hooks 的握手理由：
// 普通脚本一定早于 type="module"）。
if (window.WyckoffPendingChatHost) {
  window.WyckoffReact.mountChat(window.WyckoffPendingChatHost)
  delete window.WyckoffPendingChatHost
}

// 外壳整块由 React 渲染。shell.js（命令式模块）先跑完，所以这里能直接用它。
const shellHost = document.getElementById('root')
if (shellHost) createRoot(shellHost).render(<App />)
