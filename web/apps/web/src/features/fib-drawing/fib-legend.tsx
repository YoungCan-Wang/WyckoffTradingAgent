import { formatFibPrice, type FibChartView, type FibRole } from '@/lib/fib-drawing'
import { usePreferences, type TranslationKey } from '@/lib/preferences'

const ROLE_KEY: Record<FibRole, TranslationKey> = {
  low: 'fib.role.low',
  retracement: 'fib.role.retracement',
  roomLower: 'fib.role.roomLower',
  mid: 'fib.role.mid',
  supply: 'fib.role.supply',
  high: 'fib.role.high',
  extension: 'fib.role.extension',
}

export function FibLegend({ view }: { view: FibChartView }) {
  const { t } = usePreferences()
  const { drawing } = view
  const title = view.name ? `${view.name} ${view.code}` : view.code
  return (
    <aside className="w-full shrink-0 overflow-auto rounded-xl border border-border bg-card p-3 lg:w-72">
      <h2 className="text-sm font-semibold">{t('fib.legend')}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{title} · {view.tvSymbol}</p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">
        {t('fib.swing', {
          lowDate: drawing.lowDate,
          low: formatFibPrice(drawing.swingLow),
          highDate: drawing.highDate,
          high: formatFibPrice(drawing.swingHigh),
          count: drawing.bars,
        })}
      </p>
      <ul className="mt-3 space-y-1.5">
        {drawing.levels.map((level) => (
          <li key={level.label} className="flex items-center gap-2 text-sm">
            <span className={`h-0.5 w-8 shrink-0 ${swatchClass(level.role)}`} />
            <span className="w-14 shrink-0 tabular-nums text-muted-foreground">{level.label}</span>
            <span className="w-16 shrink-0 tabular-nums font-medium">{formatFibPrice(level.price)}</span>
            <span className="truncate text-xs text-muted-foreground">{t(ROLE_KEY[level.role])}</span>
          </li>
        ))}
      </ul>
    </aside>
  )
}

function swatchClass(role: FibRole): string {
  if (role === 'roomLower') return 'bg-amber-500'
  if (role === 'supply' || role === 'high' || role === 'extension') return 'bg-rose-400'
  return 'bg-slate-400'
}
