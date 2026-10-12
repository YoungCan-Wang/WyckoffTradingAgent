import { ArrowRight, ExternalLink } from 'lucide-react'
import { usePreferences, type TranslationKey } from '@/lib/preferences'

// 用户撞到会员墙的那一刻给的入口：整卡（二维码 + 绑定说明）用于整页锁屏，一行提示用于页内。
export function PlanetMembershipAccessCard() {
  const { t } = usePreferences()
  return (
    <aside className="rounded-lg border border-border bg-card p-5 shadow-sm">
      <div className="text-base font-semibold text-foreground">{t('tracking.locked.ctaTitle')}</div>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{t('tracking.locked.ctaDesc')}</p>
      <div className="mt-4 rounded-lg border border-border bg-white p-3">
        <img src="/zsxq_qr.jpg" alt={t('tracking.locked.qrAlt')} className="h-auto w-full rounded-md object-contain" />
      </div>
      <div className="mt-4 grid gap-2">
        <a href="/membership#capability-boundary" className="inline-flex items-center justify-center gap-2 rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-sky-700">
          {t('tracking.locked.join')}
          <ArrowRight className="h-4 w-4" />
        </a>
        <a href="https://github.com/YoungCan-Wang/WyckoffTradingAgent/blob/main/docs/COST_MODEL.md" target="_blank" rel="noreferrer" className="inline-flex items-center justify-center gap-2 rounded-lg border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted">
          {t('tracking.locked.costLink')}
          <ExternalLink className="h-4 w-4" />
        </a>
      </div>
      <p className="mt-4 text-xs leading-5 text-muted-foreground">{t('tracking.locked.memberHint')}</p>
    </aside>
  )
}

export function PlanetMembershipHint({ messageKey }: { messageKey: TranslationKey }) {
  const { t } = usePreferences()
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-800 dark:text-sky-200">
      <span>{t(messageKey)}</span>
      <a href="/membership#capability-boundary" className="inline-flex items-center gap-1 font-medium underline underline-offset-2">
        {t('membership.hint.link')}
        <ArrowRight className="h-3.5 w-3.5" />
      </a>
    </p>
  )
}
