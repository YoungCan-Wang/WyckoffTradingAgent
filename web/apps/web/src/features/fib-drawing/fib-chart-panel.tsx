import { useCallback, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { FibLegend } from '@/features/fib-drawing/fib-legend'
import { FibPriceChart } from '@/features/fib-drawing/fib-price-chart'
import type { FibChartView } from '@/lib/fib-drawing'
import { usePreferences, type ThemeMode } from '@/lib/preferences'

export function FibChartPanel({ view, loading, theme }: { view: FibChartView | null; loading: boolean; theme: ThemeMode }) {
  const { t } = usePreferences()
  const markRef = useRef('')
  const mark = view ? chartMark(view) : ''
  markRef.current = mark
  const [readyFor, setReadyFor] = useState('')
  const onReset = useCallback(() => setReadyFor(''), [])
  const onReady = useCallback(() => setReadyFor(markRef.current), [])
  const chartReady = Boolean(view) && readyFor === mark
  const cover = loading || (Boolean(view) && !chartReady)

  return (
    <div className="relative mt-4 min-h-[420px] flex-1">
      <div className="flex h-full min-h-[420px] flex-col gap-3 lg:flex-row">
        <div className="relative min-h-[420px] min-w-0 flex-1 overflow-hidden rounded-xl border border-border">
          {view && (
            <FibPriceChart
              bars={view.bars}
              drawing={view.drawing}
              theme={theme}
              roomLabel={t('fib.band.room')}
              supplyLabel={t('fib.role.supply')}
              onReset={onReset}
              onReady={onReady}
            />
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

function chartMark(view: FibChartView): string {
  const { drawing } = view
  return [view.code, drawing.bars, drawing.swingLow, drawing.swingHigh, drawing.lowDate, drawing.highDate].join('|')
}
