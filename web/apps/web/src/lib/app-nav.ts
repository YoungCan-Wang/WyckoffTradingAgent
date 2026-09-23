import { MessageSquare, Briefcase, TrendingUp, Settings, BarChart3, FileDown, Crown, Swords, History, Microscope, BookOpen, Ruler, type LucideIcon } from 'lucide-react'
import type { TranslationKey } from '@/lib/preferences'

export interface AppNavItem {
  to: string
  icon: LucideIcon
  labelKey: TranslationKey
}

export interface AppNavGroup {
  titleKey: TranslationKey
  items: readonly AppNavItem[]
}

export const APP_NAV_GROUPS = [
  {
    titleKey: 'nav.group.core',
    items: [
      { to: '/chat', icon: MessageSquare, labelKey: 'nav.chat' },
      { to: '/analysis', icon: BarChart3, labelKey: 'nav.analysis' },
      { to: '/fib', icon: Ruler, labelKey: 'nav.fib' },
      { to: '/battle', icon: Swords, labelKey: 'nav.battle' },
      { to: '/portfolio', icon: Briefcase, labelKey: 'nav.portfolio' },
    ],
  },
  {
    titleKey: 'nav.group.data',
    items: [
      { to: '/history', icon: History, labelKey: 'nav.history' },
      { to: '/export', icon: FileDown, labelKey: 'nav.export' },
    ],
  },
  {
    titleKey: 'nav.group.membership',
    items: [
      { to: '/shadow', icon: BookOpen, labelKey: 'nav.shadow' },
      { to: '/tracking', icon: TrendingUp, labelKey: 'nav.tracking' },
      { to: '/attribution', icon: Microscope, labelKey: 'nav.attribution' },
    ],
  },
  {
    titleKey: 'nav.group.system',
    items: [
      { to: '/membership', icon: Crown, labelKey: 'nav.membership' },
      { to: '/settings', icon: Settings, labelKey: 'nav.settings' },
    ],
  },
] as const satisfies readonly AppNavGroup[]
