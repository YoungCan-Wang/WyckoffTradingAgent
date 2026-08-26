import { Outlet, Link, useLocation, useNavigate } from 'react-router'
import { useCallback, useEffect, useState } from 'react'
import { MessageSquare, Briefcase, TrendingUp, Settings, LogOut, BarChart3, Moon, FileDown, BookOpen, Home, Github, Sun, Languages, Swords, History, Microscope, PanelLeftClose, PanelLeftOpen, type LucideIcon } from 'lucide-react'
import { supabase } from '@/lib/supabase'
import { useAuthStore } from '@/stores/auth'
import { MarketBar } from '@/components/market-bar'
import { usePreferences, type Locale, type TranslationKey } from '@/lib/preferences'
import { trackRouteActivity } from '@/lib/activity'
import { installWhitelistClarity } from '@/lib/product-analytics'
import { useWhitelistGate } from '@/lib/whitelist-gate'

const navGroups = [
  {
    titleKey: 'nav.group.core',
    items: [
      { to: '/chat', icon: MessageSquare, labelKey: 'nav.chat' },
      { to: '/analysis', icon: BarChart3, labelKey: 'nav.analysis' },
      { to: '/battle', icon: Swords, labelKey: 'nav.battle' },
      { to: '/portfolio', icon: Briefcase, labelKey: 'nav.portfolio' },
    ]
  },
  {
    titleKey: 'nav.group.data',
    items: [
      { to: '/history', icon: History, labelKey: 'nav.history' },
      { to: '/export', icon: FileDown, labelKey: 'nav.export' },
    ]
  },
  {
    titleKey: 'nav.group.models',
    items: [
      { to: '/tracking', icon: TrendingUp, labelKey: 'nav.tracking' },
      { to: '/attribution', icon: Microscope, labelKey: 'nav.attribution' },
    ]
  },
  {
    titleKey: 'nav.group.system',
    items: [
      { to: '/guide', icon: BookOpen, labelKey: 'nav.guide' },
      { to: '/settings', icon: Settings, labelKey: 'nav.settings' },
    ]
  }
] as const

const externalLinks = [
  { href: 'https://youngcan-wang.github.io/wyckoff-homepage/', icon: Home, labelKey: 'external.home' },
] satisfies { href: string; icon: LucideIcon; labelKey: TranslationKey }[]

const GITHUB_REPO = 'YoungCan-Wang/WyckoffTradingAgent'
const APP_SIDEBAR_STORAGE_KEY = 'wyckoff:app-sidebar-collapsed-v1'

function GitHubStarBadge({ repo }: { repo: string }) {
  return (
    <a
      href={`https://github.com/${repo}`}
      target="_blank"
      rel="noopener noreferrer"
      className="mb-3 flex w-full items-center justify-between rounded-xl border border-border bg-muted/20 px-3 py-2 text-xs transition-all hover:bg-muted/50 hover:border-primary/30 group"
    >
      <div className="flex items-center gap-2 text-muted-foreground group-hover:text-foreground transition-colors">
        <Github size={15} className="group-hover:scale-110 transition-transform duration-200" />
        <span className="font-semibold truncate">GitHub Repo</span>
      </div>
      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-bold text-primary group-hover:bg-primary group-hover:text-primary-foreground transition-all duration-200">
        Star
      </span>
    </a>
  )
}

function PreferenceControls({ collapsed = false }: { collapsed?: boolean }) {
  const controls = usePreferenceControlState()
  return collapsed ? <CollapsedPreferenceControls {...controls} /> : <ExpandedPreferenceControls {...controls} />
}

function usePreferenceControlState() {
  const { locale, setLocale, theme, toggleTheme, t } = usePreferences()
  const nextLocale: Locale = locale === 'zh-CN' ? 'en-US' : 'zh-CN'
  const ThemeIcon = theme === 'dark' ? Sun : Moon
  return { locale, nextLocale, setLocale, theme, toggleTheme, t, ThemeIcon }
}

type PreferenceControlState = ReturnType<typeof usePreferenceControlState>

function CollapsedPreferenceControls(props: PreferenceControlState) {
  return (
    <div className="mb-3 grid gap-2 px-1">
      <button
        type="button"
        onClick={props.toggleTheme}
        title={props.theme === 'dark' ? props.t('prefs.light') : props.t('prefs.dark')}
        aria-label={props.t('prefs.theme')}
        className="flex h-9 w-full items-center justify-center rounded-lg border border-border text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <props.ThemeIcon size={15} />
      </button>
      <button
        type="button"
        onClick={() => props.setLocale(props.nextLocale)}
        title={props.locale === 'zh-CN' ? props.t('prefs.switchToEnglish') : props.t('prefs.switchToChinese')}
        aria-label={props.t('prefs.language')}
        className="flex h-9 w-full items-center justify-center rounded-lg border border-border text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        {props.locale === 'zh-CN' ? 'EN' : '中'}
      </button>
    </div>
  )
}

function ExpandedPreferenceControls(props: PreferenceControlState) {
  return (
    <div className="mb-3 flex gap-2 px-3">
      <button
        type="button"
        onClick={props.toggleTheme}
        title={props.theme === 'dark' ? props.t('prefs.light') : props.t('prefs.dark')}
        aria-label={props.t('prefs.theme')}
        className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-border text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <props.ThemeIcon size={14} />
        {props.theme === 'dark' ? props.t('prefs.light') : props.t('prefs.dark')}
      </button>
      <button
        type="button"
        onClick={() => props.setLocale(props.nextLocale)}
        title={props.locale === 'zh-CN' ? props.t('prefs.switchToEnglish') : props.t('prefs.switchToChinese')}
        aria-label={props.t('prefs.language')}
        className="flex h-8 flex-1 items-center justify-center gap-1.5 rounded-lg border border-border text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <Languages size={14} />
        {props.locale === 'zh-CN' ? 'EN' : '中文'}
      </button>
    </div>
  )
}

interface SidebarFooterProps {
  collapsed: boolean
  email: string
  onLogout: () => void
}

function SidebarFooter(props: SidebarFooterProps) {
  return props.collapsed ? <CollapsedSidebarFooter onLogout={props.onLogout} /> : <ExpandedSidebarFooter email={props.email} onLogout={props.onLogout} />
}

function CollapsedSidebarFooter({ onLogout }: { onLogout: () => void }) {
  const { t } = usePreferences()
  return (
    <div className="border-t border-border p-2">
      <PreferenceControls collapsed />
      {externalLinks.map(({ href, icon: Icon, labelKey }) => (
        <a
          key={href}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={t(labelKey)}
          aria-label={t(labelKey)}
          className="mb-2 flex h-9 w-full items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <Icon size={16} />
        </a>
      ))}
      <button
        onClick={onLogout}
        title={t('action.logout')}
        aria-label={t('action.logout')}
        className="flex h-9 w-full items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <LogOut size={16} />
      </button>
    </div>
  )
}

function ExpandedSidebarFooter({ email, onLogout }: { email: string; onLogout: () => void }) {
  const { t } = usePreferences()
  return (
    <div className="border-t border-border p-3">
      <PreferenceControls />
      {externalLinks.map(({ href, icon: Icon, labelKey }) => (
        <a
          key={href}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="mb-2 flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <Icon size={14} />
          {t(labelKey)}
        </a>
      ))}
      <div className="px-3">
        <GitHubStarBadge repo={GITHUB_REPO} />
      </div>
      <div className="mb-2 truncate px-3 text-[11px] text-muted-foreground">{email}</div>
      <button
        onClick={onLogout}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <LogOut size={15} />
        {t('action.logout')}
      </button>
    </div>
  )
}

export function AppLayout() {
  const location = useLocation()
  const user = useAuthStore((s) => s.user)
  const handleLogout = useLogoutHandler()
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => readBooleanStorage(APP_SIDEBAR_STORAGE_KEY, false))
  useRouteActivity(user?.id, location)
  useWhitelistClarity(user?.id)
  const hideMarketBar = location.pathname === '/chat'
  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((value) => {
      const next = !value
      writeBooleanStorage(APP_SIDEBAR_STORAGE_KEY, next)
      return next
    })
  }, [])

  return (
    <div className="flex h-dvh overflow-hidden">
      <AppSidebar
        collapsed={sidebarCollapsed}
        email={user?.email || 'dev@preview'}
        location={location}
        onLogout={handleLogout}
        onToggle={toggleSidebar}
      />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {!hideMarketBar && <MarketBar />}
        <main className="min-h-0 flex-1 overflow-auto bg-background">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function AppSidebar({
  collapsed,
  email,
  location,
  onLogout,
  onToggle,
}: {
  collapsed: boolean
  email: string
  location: ReturnType<typeof useLocation>
  onLogout: () => void
  onToggle: () => void
}) {
  return (
    <aside className={`flex h-full shrink-0 flex-col overflow-hidden border-r border-border bg-sidebar transition-[width] duration-200 ${collapsed ? 'w-16' : 'w-56'}`}>
      <SidebarHeader collapsed={collapsed} onToggle={onToggle} />
      <SidebarNavigation collapsed={collapsed} location={location} />
      <SidebarFooter collapsed={collapsed} email={email} onLogout={onLogout} />
    </aside>
  )
}

function SidebarHeader({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const { t } = usePreferences()
  if (collapsed) {
    return (
      <div className="flex shrink-0 flex-col items-center gap-2 px-2 py-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-sm font-bold text-primary">W</div>
        <button type="button" onClick={onToggle} title="展开导航" aria-label="展开导航" className="flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground">
          <PanelLeftOpen size={16} />
        </button>
      </div>
    )
  }
  return (
    <div className="flex shrink-0 items-start justify-between gap-3 px-5 py-5">
      <div className="min-w-0">
        <h2 className="bg-gradient-to-r from-primary to-cyan-500 bg-clip-text text-xl font-bold tracking-tight text-transparent">
          Wyckoff
        </h2>
        <p className="mt-0.5 text-[11px] text-muted-foreground">{t('app.subtitle')}</p>
      </div>
      <button type="button" onClick={onToggle} title="收起导航" aria-label="收起导航" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-muted hover:text-foreground">
        <PanelLeftClose size={16} />
      </button>
    </div>
  )
}

function SidebarNavigation({ collapsed, location }: { collapsed: boolean; location: ReturnType<typeof useLocation> }) {
  const { t } = usePreferences()
  return (
    <nav className={`min-h-0 flex-1 overflow-y-auto pb-3 ${collapsed ? 'px-2 space-y-0.5' : 'px-3 space-y-3'}`}>
      {navGroups.map((group) => (
        <div key={group.titleKey} className={collapsed ? 'space-y-0.5' : 'space-y-1'}>
          {!collapsed && <div className="px-3 pt-3 pb-1 text-[9px] font-extrabold text-muted-foreground/60 uppercase tracking-widest select-none">{t(group.titleKey)}</div>}
          {group.items.map((item) => <SidebarNavLink key={item.to} item={item} collapsed={collapsed} location={location} />)}
        </div>
      ))}
    </nav>
  )
}

function SidebarNavLink({
  item,
  collapsed,
  location,
}: {
  item: (typeof navGroups)[number]['items'][number]
  collapsed: boolean
  location: ReturnType<typeof useLocation>
}) {
  const { t } = usePreferences()
  const { to, icon: Icon, labelKey } = item
  const active = _navActive(location.pathname, location.hash, to)
  return (
    <Link
      to={to}
      title={collapsed ? t(labelKey) : undefined}
      aria-label={collapsed ? t(labelKey) : undefined}
      className={`flex items-center py-2.5 text-sm transition-all border-l-2 ${collapsed ? 'justify-center rounded-lg px-2 border-transparent' : 'gap-3 px-3 pl-3.5'} ${active ? 'bg-primary/10 font-bold text-primary border-primary rounded-r-lg shadow-sm' : 'text-muted-foreground hover:bg-muted hover:text-foreground font-medium border-transparent rounded-lg'}`}
    >
      <Icon size={17} className="shrink-0" />
      {!collapsed && t(labelKey)}
    </Link>
  )
}

function readBooleanStorage(key: string, fallback: boolean): boolean {
  if (typeof window === 'undefined') return fallback
  try {
    const value = window.localStorage.getItem(key)
    if (value === 'true') return true
    if (value === 'false') return false
  } catch {
    return fallback
  }
  return fallback
}

function writeBooleanStorage(key: string, value: boolean) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(key, value ? 'true' : 'false')
  } catch {
    // localStorage may be unavailable; the sidebar still works for this session.
  }
}

function _navActive(pathname: string, hash: string, to: string) {
  const [targetPath, targetHash = ''] = to.split('#')
  if (targetHash) {
    return pathname === targetPath && hash === `#${targetHash}`
  }
  return pathname === targetPath && !hash
}

function useLogoutHandler() {
  const navigate = useNavigate()
  return async () => {
    await supabase.auth.signOut()
    navigate('/login', { replace: true })
  }
}

function useRouteActivity(userId: string | undefined, location: ReturnType<typeof useLocation>) {
  const route = `${location.pathname}${location.search}${location.hash}`
  useEffect(() => {
    if (userId) trackRouteActivity(userId, route)
  }, [route, userId])
}

function useWhitelistClarity(userId: string | undefined) {
  const whitelist = useWhitelistGate(userId)
  useEffect(() => {
    if (userId && whitelist.data === true) installWhitelistClarity(userId)
  }, [userId, whitelist.data])
}
