/**
 * 应用外壳：侧栏 + 顶栏 + 主区（会话 / 目标页面）+ 菜单 + 设置弹窗。
 *
 * 产物面板、K 线、内置浏览器仍由命令式模块负责（canvas 绘图、原生 view 几何、
 * sandbox iframe）—— 那些本质不是声明式的，套一层 React 只会多一层壳。
 * 它们通过 window.WyckoffShell 暴露给这里调用。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { collect } from '../lib/ipc'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { OpenMenu, type MenuEntry } from './OpenMenu'
import { AccountMenu } from './AccountMenu'
import { SettingsModal } from './SettingsModal'
import { ChatView } from './ChatView'
import { PagePane } from './PagePane'

const t = (key: string) => window.WyckoffI18n.t(key)

const SIDE_KEY = 'wyckoff.sidebar'

/** 每个视图的标题 key —— 顶栏用它显示当前位置。 */
const TITLES: Record<string, string> = {
  chat: 'nav.today',
  tasks: 'nav.tasks',
  approvals: 'nav.approvals',
  portfolio: 'nav.portfolio',
  schedules: 'nav.schedules',
  tracking: 'nav.tracking',
  attribution: 'nav.attribution',
  reports: 'nav.reports'
}

export function App () {
  const [view, setView] = useState('chat')
  const [sideOpen, setSideOpen] = useState(() => {
    try {
      const saved = localStorage.getItem(SIDE_KEY)
      if (saved !== null) return saved === '1'
    } catch { /* 私密模式 */ }
    // 常规桌面窗口有空间放导航；窄窗口内容优先。选过一次之后用户的偏好为准。
    return window.innerWidth >= 1180
  })
  const [backendState, setBackendState] = useState('starting')
  const [counts, setCounts] = useState({ approvals: 0, schedules: 0 })
  const [account, setAccount] = useState({ signedIn: false, email: '', userId: '' })
  const [openAnchor, setOpenAnchor] = useState<HTMLElement | null>(null)
  const [acctAnchor, setAcctAnchor] = useState<HTMLElement | null>(null)
  const [settings, setSettings] = useState<{ open: boolean; section: string; anchor?: string }>(
    { open: false, section: 'general' }
  )
  const opener = useRef<HTMLElement | null>(null)
  // 上一次看到的账号 —— 用来发现「换人了」（登录、退出、换账号登录都算）
  const lastUser = useRef<string | null>(null)

  useEffect(() => {
    try { localStorage.setItem(SIDE_KEY, sideOpen ? '1' : '0') } catch { /* 私密模式 */ }
    // 侧栏动画结束后原生浏览器 view 的几何要跟上（它只是浮在窗口上）。
    const timer = setTimeout(() => window.WyckoffShell?.syncBrowser?.(), 220)
    return () => clearTimeout(timer)
  }, [sideOpen])

  const refreshCounts = useCallback(async () => {
    const [ap, sc] = await Promise.all([
      collect('approve_list').catch(() => null),
      collect('schedules').catch(() => null)
    ])
    setCounts({
      approvals: Number((ap as { count?: number } | null)?.count || 0),
      schedules: ((sc as { schedules?: Array<{ enabled?: boolean }> } | null)?.schedules || [])
        .filter((s) => s.enabled).length
    })
  }, [])

  const loadAccount = useCallback(async () => {
    const data = await collect('account').catch(() => null)
    const d = (data || {}) as { signed_in?: boolean; email?: string; user_id?: string }
    const uid = String(d.user_id || '')
    // 账号变了就清缓存 + 通知已挂载的页面清 state。只清缓存挡得住「下次进页面」，
    // 挡不住「此刻正看着持仓页」。
    if (lastUser.current !== null && lastUser.current !== uid) {
      window.WyckoffReact?.clearPortfolioCaches?.()
      window.dispatchEvent(new CustomEvent('wyckoff:account-changed', { detail: { userId: uid } }))
    }
    lastUser.current = uid
    setAccount({ signedIn: Boolean(d.signed_in), email: String(d.email || ''), userId: uid })
  }, [])

  // 桥的状态机。ready 的那一刻才去拉侧栏计数与账号。
  useEffect(() => {
    let wasReady = false
    const apply = (state: string) => {
      setBackendState(state)
      if (state !== 'ready') return
      void refreshCounts()
      void loadAccount()
      void window.WyckoffShell?.loadAppearance?.()
      wasReady = true
    }
    void window.wyckoff.status().then((s) => apply(s.state))
    window.wyckoff.onStatus((s) => {
      if (s.state === 'log') return
      // 对话进行中重启不该打断记录，所以只在进入 ready 的那次拉数据。
      if (s.state === 'ready' && wasReady) { setBackendState('ready'); return }
      apply(s.state)
    })
  }, [refreshCounts, loadAccount])

  const navigate = useCallback((next: string) => {
    setView(next)
    setOpenAnchor(null)
    setAcctAnchor(null)
  }, [])

  const openSettings = useCallback((section = 'general', anchor?: string) => {
    opener.current = (document.activeElement as HTMLElement) || null
    setSettings({ open: true, section, anchor })
  }, [])

  const closeSettings = useCallback(() => {
    setSettings((s) => ({ ...s, open: false, anchor: undefined }))
    const target = opener.current
    opener.current = null
    if (target?.isConnected) target.focus()
  }, [])

  const signOut = useCallback(async () => {
    if (!window.confirm(t('signin.signoutConfirm'))) return
    const res = await collect('sign_out').catch(() => null)
    if (!res) {
      window.WyckoffChat?.sysLine?.(t('signin.signoutFailed'), true)
      return
    }
    // 退出即清掉所有账号的持仓缓存 —— 留着等于把上一个人的持仓存在这台机器上。
    window.WyckoffReact?.clearPortfolioCaches?.()
    closeSettings()
    await loadAccount()
    window.WyckoffChat?.sysLine?.(t('signin.signedOutDone'))
  }, [closeSettings, loadAccount])

  // app.js 时代的 WyckoffApp 桥继续保留：命令式模块（K 线、报告面板）与
  // React 页面互相回调都走它。
  useEffect(() => {
    window.WyckoffApp = {
      navigate,
      refreshApprovals: () => { void refreshCounts() },
      refreshSchedules: () => { void refreshCounts() },
      openKline: (code) => window.WyckoffShell?.openKline?.(code),
      openReport: (title, body) => window.WyckoffShell?.openReport?.(title, body),
      refreshCharts: (codes) => window.WyckoffShell?.refreshCharts?.(codes),
      openSettings,
      sysLine: (text, isError) => window.WyckoffChat?.sysLine?.(text, Boolean(isError)),
      getSendOnEnter: () => window.WyckoffShell?.getSendOnEnter?.() ?? true
    }
  }, [navigate, refreshCounts, openSettings])

  const toggle = (
    <button
      className="icb side-toggle"
      type="button"
      title={t(sideOpen ? 'tooltip.sidebarHide' : 'tooltip.sidebarShow')}
      onClick={() => setSideOpen(!sideOpen)}
    >
      <i className="side-toggle-open" data-lucide="panel-left-open" aria-hidden="true" />
      <i className="side-toggle-close" data-lucide="panel-left-close" aria-hidden="true" />
    </button>
  )

  const menuEntries: MenuEntry[] = [
    { id: 'reports', icon: 'file-text', labelKey: 'menu.reports', shortcut: '⌘R', onPick: () => navigate('reports') },
    { id: 'chart', icon: 'chart-candlestick', labelKey: 'menu.chart', shortcut: '⌘K', onPick: () => window.WyckoffShell?.promptKline?.() },
    { id: 'browser', icon: 'globe-2', labelKey: 'menu.browser', shortcut: '⌘T', onPick: () => window.WyckoffShell?.openBrowser?.() },
    { id: 'pane', icon: 'panel-right-open', labelKey: 'menu.pane', shortcut: '⌘⌥B', separated: true, onPick: () => window.WyckoffShell?.togglePane?.() }
  ]

  return (
    <>
      {sideOpen ? (
        <Sidebar
          active={view}
          onNavigate={navigate}
          counts={counts}
          accountLabel={account.signedIn ? (account.email || t('account.signedIn')) : t('account.signedOut')}
          accountInitial={account.email ? account.email[0] : '·'}
          onAccountClick={setAcctAnchor}
          toggleSlot={toggle}
        />
      ) : null}

      <main className="thread">
        <TopBar
          titleKey={TITLES[view] || 'nav.today'}
          pendingCount={counts.approvals}
          backendState={backendState}
          onPendingClick={() => navigate('approvals')}
          onRestart={() => { void window.wyckoff.restart() }}
          onOpenMenu={setOpenAnchor}
          toggleSlot={sideOpen ? null : toggle}
        />
        {/* 会话区长驻不卸载：切到别的页面再回来，对话记录还在。 */}
        <div className="tscroll" id="stream" hidden={view !== 'chat'}>
          <ChatView />
        </div>
        {view !== 'chat' ? <PagePane view={view} /> : null}
      </main>

      <OpenMenu anchor={openAnchor} entries={menuEntries} onClose={() => setOpenAnchor(null)} />
      <AccountMenu
        anchor={acctAnchor}
        label={account.signedIn ? (account.email || t('account.signedIn')) : t('account.signedOut')}
        initial={account.email ? account.email[0] : '·'}
        signedIn={account.signedIn}
        onClose={() => setAcctAnchor(null)}
        onSettings={() => openSettings()}
        onSignOut={() => void signOut()}
      />
      <SettingsModal
        open={settings.open}
        section={settings.section}
        anchor={settings.anchor}
        onSection={(sec) => setSettings((s) => ({ ...s, section: sec, anchor: undefined }))}
        onClose={closeSettings}
        onMessage={(text, isError) => window.WyckoffChat?.sysLine?.(text, isError)}
        onSignOut={() => void signOut()}
        onConfigChanged={() => {
          window.dispatchEvent(new Event('wyckoff:models-changed'))
          window.dispatchEvent(new Event('wyckoff:settings-changed'))
          void window.WyckoffShell?.loadAppearance?.()
        }}
      />
    </>
  )
}
