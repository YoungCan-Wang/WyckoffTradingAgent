import { Link } from 'react-router'
import { financialValueClass } from '@/lib/financial-colors'
import { formatSignedPercent } from '@/lib/format'
import type { ShadowLedgerShowcase } from '@/lib/shadow-ledger-api'
import { ShadowNavChart } from './nav-chart'
import type { ShadowCopy } from './copy'

export function ShadowShowcasePanel({
  showcase,
  copy,
  showCta,
}: {
  showcase: ShadowLedgerShowcase
  copy: ShadowCopy
  showCta: boolean
}) {
  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-sm font-semibold">{copy.navTitle}</h2>
        {showcase.navCurve.length > 0 ? <div className="mt-3"><ShadowNavChart curve={showcase.navCurve} /></div> : (
          <p className="mt-3 text-sm text-muted-foreground">{copy.empty}</p>
        )}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Stat label={copy.periodPnl} value={formatMoney(showcase.periodPnlAmount)} tone={showcase.periodPnlAmount} />
        <Stat label={copy.periodPct} value={formatSignedPercent(showcase.periodPnlPct)} tone={showcase.periodPnlPct} />
        <Stat label={copy.maxDrawdown} value={formatSignedPercent(-Math.abs(showcase.maxDrawdownPct))} tone={-1} />
        <Stat
          label={copy.winRate}
          value={showcase.winRatePct == null ? '—' : formatSignedPercent(showcase.winRatePct, 1)}
          hint={showcase.winRateNote ?? undefined}
        />
        <Stat label={copy.openCount} value={String(showcase.openPositionCount)} />
      </div>
      <div className="rounded-lg border border-border bg-card px-4 py-3">
        <div className="text-xs text-muted-foreground">{copy.sectors}</div>
        <div className="mt-2 flex flex-wrap gap-2">
          {showcase.sectorTags.length === 0 ? <span className="text-sm text-muted-foreground">—</span> : showcase.sectorTags.map((tag) => (
            <span key={tag} className="rounded-full bg-muted px-2.5 py-1 text-xs font-medium">{tag}</span>
          ))}
        </div>
      </div>
      {showCta && <MembershipCta copy={copy} />}
    </section>
  )
}

function MembershipCta({ copy }: { copy: ShadowCopy }) {
  return (
    <aside className="rounded-xl border border-amber-300/40 bg-amber-500/5 p-5">
      <h3 className="text-base font-semibold">{copy.ctaTitle}</h3>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{copy.ctaDesc}</p>
      <Link
        to="/membership"
        className="mt-4 inline-flex rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
      >
        {copy.cta}
      </Link>
    </aside>
  )
}

function Stat({ label, value, tone, hint }: { label: string; value: string; tone?: number; hint?: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${tone == null ? '' : financialValueClass(tone)}`}>{value}</div>
      {hint && <div className="mt-1 text-[11px] leading-5 text-muted-foreground">{hint}</div>}
    </div>
  )
}

export function formatMoney(value: number): string {
  const abs = Math.abs(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (value > 0) return `+${abs}`
  if (value < 0) return `-${abs}`
  return abs
}
