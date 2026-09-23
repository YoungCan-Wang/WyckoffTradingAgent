import { useCallback, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { FibLegend } from '@/features/fib-drawing/fib-legend'
import { FibOverlay } from '@/features/fib-drawing/fib-overlay'
import { TradingViewChart } from '@/features/fib-drawing/tradingview-chart'
import type { FibChartView } from '@/lib/fib-drawing'
import { usePreferences, type Locale, type ThemeMode } from '@/lib/preferences'

export function FibChartPanel({
  view,
  loading,
  theme,
  locale,
}: {
  view: FibChartView | null
  loading: boolean
  theme: ThemeMode
  locale: Locale
}) {
  const { t } = usePreferences()
  const symbolRef = useRef('')
  symbolRef.current = view?.tvSymbol ?? ''
  const [readyFor, setReadyFor] = useState('')
  const onReset = useCallback(() => setReadyFor(''), [])
  const onReady = useCallback(() => setReadyFor(symbolRef.current), [])
  const chartReady = Boolean(view) && readyFor === view?.tvSymbol
  const reveal = Boolean(view) && chartReady && !loading
  const cover = loading || (Boolean(view) && !chartReady)

  return (
    <div className="relative mt-4 min-h-[420px] flex-1">
      <div className="flex h-full min-h-[420px] flex-col gap-3 lg:flex-row">
        <div className="relative min-h-[420px] min-w-0 flex-1 rounded-xl border border-border">
          {view && (
            <>
              <TradingViewChart symbol={view.tvSymbol} theme={theme} locale={locale} onReset={onReset} onReady={onReady} />
              {reveal && (
                <FibOverlay drawing={view.drawing} roomLabel={t('fib.band.room')} supplyLabel={t('fib.role.supply')} />
              )}
            </>
          )}
        </div>
        {view && <FibLegend view={view} />}
      </div>
      {!view && !loading && (
        <div className="absolute inset-0 flex items-center justify-center px-6 text-sm text-muted-foreground">{t('fib.empty')}</div>
      )}
      {cover && (
        <div className="absolute inset-0 z-20 flex items-center justify-center gap-2 bg-background text-sm text-muted-foreground">
          <Loader2 size={18} className="animate-spin" />
          {t('common.loading')}
        </div>
      )}
    </div>
  )
}
