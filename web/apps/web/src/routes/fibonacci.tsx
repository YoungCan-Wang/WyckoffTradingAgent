import { Loader2 } from 'lucide-react'
import { StockSearchBox, useStockSearch } from '@/components/stock-search-box'
import { FibChartPanel } from '@/features/fib-drawing/fib-chart-panel'
import { FibPresetBar } from '@/features/fib-drawing/fib-preset-bar'
import { useFibGenerate } from '@/features/fib-drawing/use-fib-generate'
import { usePreferences } from '@/lib/preferences'

export function FibonacciPage() {
  const { t, theme, locale } = usePreferences()
  const search = useStockSearch('analysis')
  const drawing = useFibGenerate(search)
  const disabled = drawing.loading || !search.symbol.trim()

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden px-6 py-5">
      <div className="shrink-0 border-b border-border/70 pb-4">
        <h1 className="mb-1 text-xl font-semibold">{t('fib.title')}</h1>
        <p className="mb-4 text-sm text-muted-foreground">{t('fib.intro')}</p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[240px] flex-1 lg:max-w-3xl">
            <StockSearchBox
              search={search}
              onSubmit={() => void drawing.generate()}
              onClearError={drawing.clearError}
              placeholder={t('fib.searchPlaceholder')}
              listboxId="fib-stock-search"
            />
          </div>
          <button
            type="button"
            onClick={() => void drawing.generate()}
            disabled={disabled}
            className="flex h-10 shrink-0 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {drawing.loading && <Loader2 size={16} className="animate-spin" />}
            {drawing.loading ? t('fib.generating') : t('fib.generate')}
          </button>
        </div>
        <div className="mt-3">
          <FibPresetBar preset={drawing.preset} onChange={drawing.selectPreset} />
        </div>
        {drawing.error && (
          <div className="mt-3 rounded-lg bg-red-50 px-4 py-2.5 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-200">
            {drawing.error}
          </div>
        )}
      </div>
      <FibChartPanel view={drawing.view} loading={drawing.loading} theme={theme} locale={locale} />
    </div>
  )
}
