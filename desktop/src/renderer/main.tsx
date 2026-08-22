/** React 外壳入口，并为命令式图表/报告模块暴露少量互操作能力。 */
import { createRoot } from 'react-dom/client'
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
import { App } from './components/App'
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

declare global {
  interface Window {
    WyckoffReact: {
      /** 对话里跑了改持仓的工具时调用，作废本地缓存。 */
      invalidatePortfolioCache: (toolName: string) => void
      /** 登录态变化时调用：清掉所有账号的持仓缓存。 */
      clearPortfolioCaches: () => void
    }
    WyckoffRefreshIcons: () => void
    /**
     * React 页面与命令式图表、报告模块之间的动作桥。
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
    /** 设置、模型切换等动作往对话流写系统提示。 */
    WyckoffChat?: {
      sysLine: (text: string, isError?: boolean) => void
      newAnalysis: () => Promise<boolean>
    }
    /** 把新插入的 data-lucide 占位符换成 svg。 */
    WyckoffIcons?: { render: () => void }
    /**
     * 命令式外壳（shell.js）：产物面板、K 线、内置浏览器、外观。
     * 这些不用 React —— canvas 绘图、原生 view 几何、sandbox iframe 都是
     * 命令式的，套一层壳不会让代码更好。
     */
    WyckoffShell?: {
      /**
       * 统一的产物打开入口。收口 openKline / openReport —— 调用方只说
       * 「打开这个产物」,哪种 kind 用哪个渲染器由 shell 决定。
       */
      openArtifact?: (artifact: {
        id: string
        kind: string
        title: string
        payload: Record<string, unknown>
      }) => void
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

window.WyckoffReact = {
  invalidatePortfolioCache: (toolName: string) => {
    // 全清而不是只清当前账号：这里拿不到 user_id（同步调用），而缓存本来就是
    // 可丢的。多清的代价是别的账号下次进页面重拉一次。
    if (isPortfolioWriteTool(toolName)) clearAllCaches()
  },
  clearPortfolioCaches: clearAllCaches
}
window.WyckoffRefreshIcons = refreshIcons

// 外壳整块由 React 渲染。shell.js（命令式模块）先跑完，所以这里能直接用它。
const shellHost = document.getElementById('root')
if (shellHost) createRoot(shellHost).render(<App />)
