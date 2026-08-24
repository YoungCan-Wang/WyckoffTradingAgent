/**
 * 应用外壳：侧栏 + 顶栏 + 主区（会话 / 目标页面）+ 菜单 + 设置弹窗。
 *
 * 产物面板、K 线、内置浏览器仍由命令式模块负责（canvas 绘图、原生 view 几何、
 * sandbox iframe）—— 那些本质不是声明式的，套一层 React 只会多一层壳。
 * 它们通过 window.WyckoffShell 暴露给这里调用。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { clearIpcCache } from '../lib/ipcCache'
import { collect } from '../lib/ipc'
import { LoginModal } from './LoginModal'
import { Toast, type ToastMessage } from './Toast'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { OpenMenu, type MenuEntry } from './OpenMenu'
import { AccountMenu } from './AccountMenu'
import { SettingsModal } from './SettingsModal'
import { ChatView } from './ChatView'
import { PagePane } from './PagePane'
import SessionList from './SessionList'

const t = (key: string) => window.WyckoffI18n.t(key)
// 带参版本。会话列表要插条数（「12 条消息」），现有的 t 只接 key。
const tp = (key: string, params?: Record<string, string>) => window.WyckoffI18n.t(key, params)

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
  // 当前会话与列表刷新计数。放在 App 而不是 SessionList 里：切换会话要由
  // 侧边栏发起、由 ChatView 执行，两边都不拥有这个状态。
  const [activeSession, setActiveSession] = useState('')
  const [sessionNonce, setSessionNonce] = useState(0)
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
  // checked 区分「查过了、确实没登录」与「还没查」。少了它，已登录用户在
  // account 返回之前会先闪一下登录页 —— 那比慢一点更糟。
  const [account, setAccount] = useState({ signedIn: false, email: '', userId: '', checked: false, lastEmail: '' })
  const [openAnchor, setOpenAnchor] = useState<HTMLElement | null>(null)
  const [acctAnchor, setAcctAnchor] = useState<HTMLElement | null>(null)
  const [loginOpen, setLoginOpen] = useState(false)
  const [toasts, setToasts] = useState<ToastMessage[]>([])
  // 账号类反馈用轻提示，不塞进对话流 —— 那是用户与模型的往来记录。
  const toast = useCallback((text: string, error = false) => {
    setToasts((prev) => [...prev, { id: Date.now() + Math.random(), text, error }])
  }, [])
  const dropToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((m) => m.id !== id))
  }, [])
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
    // 拿不到后端时也必须把 checked 置真。
    //
    // `checked` 为假时整个应用刻意什么都不渲染（避免已登录用户闪一下登录页），
    // 而这个函数只在桥进入 ready 时被调 —— 后端起不来的话 `checked` 永远是假，
    // **界面永久空白**。CI 上就是这样：没有 Python payload，`.shell-root` 挂上了
    // 但里面是空的（诊断显示 shellChildren: []），侧栏和顶栏一个都没有。
    //
    // 后端起不来是真实场景（安装不完整、Python 崩了），那时用户该看到登录页
    // 或错误态，不该看到一片空白。所以 catch 分支也走 setAccount。
    // E2E 旁路：CI 上没有 Python payload，后端起不来 —— 所以这个判断**必须**
    // 在渲染侧。放在 Python 的 account 方法里时它永远不会被执行（诊断显示界面
    // 停在登录页），因为那个方法压根没被调到。
    if (window.wyckoff?.e2eFakeSignin) {
      setAccount({
        signedIn: true, email: 'e2e@example.com', userId: 'e2e-user',
        checked: true, lastEmail: ''
      })
      return
    }
    const data = await collect('account').catch(() => null)
    const d = (data || {}) as { signed_in?: boolean; email?: string; user_id?: string; last_email?: string }
    const uid = String(d.user_id || '')
    // 账号变了就清缓存 + 通知已挂载的页面清 state。只清缓存挡得住「下次进页面」，
    // 挡不住「此刻正看着持仓页」。
    if (lastUser.current !== null && lastUser.current !== uid) {
      window.WyckoffReact?.clearPortfolioCaches?.()
      // 跟踪记录、归因报告这些也要清:它们缓存在模块作用域,不随组件卸载消失,
      // 换账号后不清就会把上一个人的数据显示给新登录的人。
      clearIpcCache()
      window.dispatchEvent(new CustomEvent('wyckoff:account-changed', { detail: { userId: uid } }))
    }
    lastUser.current = uid
    setAccount({
      signedIn: Boolean(d.signed_in),
      email: String(d.email || ''),
      userId: uid,
      checked: true,
      lastEmail: String(d.last_email || '')
    })
  }, [])

  // 登录态必须**独立于后端状态**查一次。
  //
  // 它原来只在桥进入 ready 时才查，而 `account.checked` 为假时整个应用刻意什么
  // 都不渲染（避免已登录用户闪一下登录页）。两条合起来的后果：后端起不来时
  // **界面永久空白** —— `.shell-root` 挂上了但里面是空的。
  //
  // CI 上稳定复现（没有 Python payload），诊断显示 `shellChildren: []`、
  // topbar/thread 都不存在。这不只是测试问题：安装不完整或 Python 崩掉时，
  // 用户会看到一片空白而不是登录页或错误态。
  //
  // 所以这里无条件查一次；ready 之后再查一次（那时才可能有真实 session）。
  useEffect(() => {
    void loadAccount()
  }, [loadAccount])

  // 桥的状态机。ready 的那一刻才去拉侧栏计数与账号。
  useEffect(() => {
    let wasReady = false
    const apply = (state: string) => {
      setBackendState(state)
      if (state !== 'ready') return
      void refreshCounts()
      void loadAccount()
      void window.WyckoffShell?.loadAppearance?.()
      // 通知模型选择器重读配置。它只在挂载时拉一次，而打包后 Python 要几秒才起来
      // —— 那一次必然落空，然后它再没有第二次机会，界面就永远停在「未配置模型」。
      // 开发环境后端已经在跑，所以这个缺口一直没露出来。
      window.dispatchEvent(new Event('wyckoff:models-changed'))
      wasReady = true
    }
    void window.wyckoff.status().then((s) => apply(s.state))
    const off = window.wyckoff.onStatus((s) => {
      if (s.state === 'log') return
      // 对话进行中重启不该打断记录，所以只在进入 ready 的那次拉数据。
      if (s.state === 'ready' && wasReady) { setBackendState('ready'); return }
      apply(s.state)
    })
    return off
  }, [refreshCounts, loadAccount])

  const navigate = useCallback((next: string) => {
    setView(next)
    setOpenAnchor(null)
    setAcctAnchor(null)
  }, [])

  const newAnalysis = useCallback(async () => {
    navigate('chat')
    await window.WyckoffChat?.newAnalysis()
    // 让会话列表重拉：新会话要立刻出现，否则用户以为按钮没生效。
    setSessionNonce((n) => n + 1)
    setActiveSession(window.WyckoffChat?.sessionId?.() || '')
  }, [navigate])

  const switchSession = useCallback(async (id: string) => {
    navigate('chat')
    const ok = await window.WyckoffChat?.loadSession(id)
    if (ok) setActiveSession(id)
  }, [navigate])

  const openSettings = useCallback((section = 'general', anchor?: string) => {
    opener.current = (document.activeElement as HTMLElement) || null
    setSettings({ open: true, section, anchor })
  }, [])

  const closeSettings = useCallback(() => {
    setSettings((s) => ({ ...s, open: false, anchor: undefined }))
    const target = opener.current
    opener.current = null
    // 先让 React 卸掉弹窗并解除背景 inert，再把焦点还给触发控件。
    requestAnimationFrame(() => { if (target?.isConnected) target.focus() })
  }, [])

  const signOut = useCallback(async () => {
    if (!window.confirm(t('signin.signoutConfirm'))) return
    const res = await collect('sign_out').catch(() => null)
    if (!res) {
      toast(t('signin.signoutFailed'), true)
      return
    }
    // 退出即清掉所有账号的持仓缓存 —— 留着等于把上一个人的持仓存在这台机器上。
    window.WyckoffReact?.clearPortfolioCaches?.()
    closeSettings()
    await loadAccount()
    toast(t('signin.signedOutDone'))
  }, [closeSettings, loadAccount, toast])

  // 命令式模块（K 线、报告面板）与 React 页面互相回调都走这一条窄桥。
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
    { id: 'chart', icon: 'chart-candlestick', labelKey: 'menu.chart', shortcut: '⌘K', onPick: (anchor) => window.WyckoffShell?.promptKline?.(anchor) },
    { id: 'browser', icon: 'globe-2', labelKey: 'menu.browser', shortcut: '⌘T', onPick: () => window.WyckoffShell?.openBrowser?.() },
    { id: 'pane', icon: 'panel-right-open', labelKey: 'menu.pane', shortcut: '⌘⌥B', separated: true, onPick: () => window.WyckoffShell?.togglePane?.() }
  ]

  const onSignedIn = useCallback(
    async (info: { email: string; synced?: Record<string, unknown> }) => {
      // 重新走一遍 loadAccount 而不是直接 setAccount：那条路径还负责清跨账号
      // 缓存并广播 account-changed，绕过去会让上一个账号的持仓留在界面上。
      await loadAccount()
      await refreshCounts()
      const synced = info.synced as { models?: string[]; data_sources?: string[] } | undefined
      const count = (synced?.models?.length || 0) + (synced?.data_sources?.length || 0)
      // 登录成功**一定**要有反馈。原来只在 count > 0 时提示，云端没配过东西
      // 或本地已经都有时就完全静默 —— 弹窗一关，用户不知道成了没有。
      // 同步到东西就顺带报一下数量，那是附加信息，不是提示存在的前提。
      toast(count > 0
        ? t('login.okSynced').replace('{count}', String(count))
        : t('login.ok'))
    },
    [loadAccount, refreshCounts, toast]
  )

  // 未登录也能用：持仓存本地、模型可本地配。登录只是额外接上云端，
  // 所以它是侧栏账号行里的一个动作，不是进门必答题。
  //
  // 但账号态查明之前仍然不渲染：否则已登录用户会先闪一下「未登录」。
  if (!account.checked) return null

  return (
    <>
      {/*
        产物自动展开时宣告给读屏。
        刻意**不移动焦点**：用户可能正在输入框里打字,抢焦点是最糟的打断。
        polite 而不是 assertive —— 这是提示,不是警报。
      */}
      <div id="artifact-live" className="sr-only" aria-live="polite" role="status" />
      {sideOpen ? (
        <Sidebar
          active={view}
          onNavigate={navigate}
          onNewAnalysis={newAnalysis}
          sessionSlot={
            <SessionList
              activeId={activeSession}
              onSwitch={switchSession}
              onNeedNew={newAnalysis}
              reloadKey={sessionNonce}
              t={tp}
            />
          }
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
        onSignIn={() => setLoginOpen(true)}
      />

      <Toast items={toasts} onExpire={dropToast} />

      {/* 登录弹窗：从侧栏账号行打开。未登录也能用应用，所以它不是进门关卡。 */}
      <LoginModal
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        collect={collect}
        onSignedIn={onSignedIn}
        backendReady={backendState === 'ready'}
        initialEmail={account.lastEmail}
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
